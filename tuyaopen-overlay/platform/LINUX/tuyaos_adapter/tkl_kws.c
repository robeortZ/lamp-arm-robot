/**
 * @file tkl_kws.c
 * @version 0.1
 * @date 2025-04-08
 */

#include "tuya_cloud_types.h"

#include "tkl_memory.h"
#include "tkl_system.h"
#include "tkl_thread.h"
#include "tkl_mutex.h"
#include "tkl_semaphore.h"
#include "tuya_ringbuf.h"
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* [PATCH-SHERPA-WAKEUP] 外部唤醒 socket 依赖 */
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/stat.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>

#include "tkl_kws.h"

#if defined(ENABLE_AUDIO_ALSA) && (ENABLE_AUDIO_ALSA == 1)
#include "tdd_audio_alsa.h"
#endif

#include "kws/audio_subsys_kws_factory2.h"

/***********************************************************
************************macro define************************
***********************************************************/
// mono 16k 16bit, 20ms frame -> 320bytes
#define TKL_KWS_FRAME_SIZE   (320 * 2) // 16bit
#define KWS_RING_BUFF_LEN    (TKL_KWS_FRAME_SIZE * (2000 / 20)) // 2s buffer for 16kHz, 16bit audio

#define MODEL_CHUNK_TIME_MS   (300) // 300ms chunk for kws model
#define MODEL_CHUNK_SIZE      (MODEL_CHUNK_TIME_MS / 20 * TKL_KWS_FRAME_SIZE) // 16kHz, 16bit

/* [PATCH-SHERPA-WAKEUP] 外部唤醒 socket 路径（sherpa 进程检测到唤醒词后 connect 此路径触发唤醒） */
#define EXTERNAL_WAKEUP_SOCK  "/run/tuya_wakeup.sock"

/* [PATCH-SHERPA-WAKEUP] 麦克风音频转发 socket：sherpa 进程连接后接收主程序采集的 PCM 流，
 * 从而避免 sherpa 自己 arecord 与主程序争抢 plughw:0,0（声卡独占）。
 * 数据格式：16k/mono/S16_LE 原始 PCM 流。 */
#define AUDIO_FEED_SOCK  "/run/tuya_wakeup_audio.sock"

/***********************************************************
***********************typedef define***********************
***********************************************************/
typedef struct {
    uint32_t   w32WakeWord;
    char *asr_txt;
    TKL_KWS_WAKEUP_WORD_E wakeup_word;
} ASR_WAKEUP_WORD_MAP_T;

typedef struct {
    audio_subsys_module_t* module;
    TKL_KWS_WAKEUP_CB wakeup_cb;
    TKL_THREAD_HANDLE kws_thread;
    TKL_THREAD_HANDLE ext_wakeup_thread;
    TKL_THREAD_HANDLE audio_feed_thread;
    int  ext_wakeup_fd;
    int  audio_feed_fd;      /* 当前已连接的 sherpa 音频客户端 fd（-1 表示无） */
    TKL_MUTEX_HANDLE audio_feed_mutex;
    TUYA_RINGBUFF_T mic_ringbuf;
} TKL_KWS_CTX_S;

/***********************************************************
***********************variable define**********************
***********************************************************/

static ASR_WAKEUP_WORD_MAP_T cASR_WAKEUP_WORD_MAP[] = {
    {1, "你好涂鸦",    TKL_KWS_WAKEUP_NIHAO_TUYA}
};

static TKL_KWS_CTX_S g_kws_ctx = {0};

static void __expand_user_path(const char *src, char *dst, size_t dst_size)
{
    if (src == NULL || dst == NULL || dst_size == 0) {
        return;
    }

    if (src[0] == '~' && src[1] == '/') {
        const char *home = getenv("HOME");
        if (home != NULL && home[0] != '\0') {
            snprintf(dst, dst_size, "%s/%s", home, src + 2);
            return;
        }
    }

    snprintf(dst, dst_size, "%s", src);
}

/***********************************************************
***********************function define**********************
***********************************************************/
void __tkl_kws_process(int16_t *mic_data, int16_t *ref_data, int16_t *out_data, uint32_t frames)
{
    /* [PATCH-SHERPA-WAKEUP] 调试：统计 frames 值（每 1000 次打印一次） */
    static uint32_t dbg_cnt = 0;
    dbg_cnt++;
    if (dbg_cnt % 1000 == 0) {
        printf("[kws-feed] dbg: call#%u frames=%u (bytes=%u)\n", dbg_cnt, frames, frames * 2);
    }

    /* [PATCH-SHERPA-WAKEUP] 把麦克风 PCM 转发给 sherpa 音频客户端（若有连接）。
     * 注意：采样率统一由 tdd_audio_alsa.c 采集端 2:1 降采样修复（PATCH-LAMP-32KDS），
     * 这里收到的已经是真 16k 数据，直接转发，不要在这里做二次降采样。 */
    int fd = g_kws_ctx.audio_feed_fd;
    if (fd > 0) {
        ssize_t w = send(fd, mic_data, frames * 2, MSG_NOSIGNAL | MSG_DONTWAIT);
        if (w < 0) {
            int e = errno;
            if (e != EAGAIN && e != EWOULDBLOCK && e != EINTR) {
                printf("[kws-feed] send failed fd=%d err=%d(%s), closing\n", fd, e, strerror(e));
                close(fd);
                if (g_kws_ctx.audio_feed_fd == fd) {
                    g_kws_ctx.audio_feed_fd = -1;
                }
            }
        }
    }

    if (g_kws_ctx.module == NULL || g_kws_ctx.mic_ringbuf == NULL) {
        return;
    }

    uint32_t rb_free = tuya_ring_buff_free_size_get(g_kws_ctx.mic_ringbuf);
    if (rb_free < frames * 2) {
        // ringbuf full, discard data
        return;
    }
    tuya_ring_buff_write(g_kws_ctx.mic_ringbuf, (uint8_t *)mic_data, frames * 2);

    return;
}

static int __tkl_kws_result(void* data, int dlen, int flag, void* userdata) {
    /* [PATCH-SHERPA-WAKEUP] 开机前 3 秒内 DC 数据会误报命中（score 0.4~0.92），
     * 直接忽略，避免假唤醒（并规避在模式未初始化时触发唤醒回调导致的偶发 SEGV）。 */
    static time_t first_result_ts = 0;
    if (first_result_ts == 0) {
        first_result_ts = time(NULL);
    }
    if (flag == 1 && g_kws_ctx.wakeup_cb != NULL) {
        if (time(NULL) - first_result_ts < 3) {
            printf("[kws] ignore fake keyword hit within 3s of boot\n");
            return 0;
        }
        g_kws_ctx.wakeup_cb(TKL_KWS_WAKEUP_NIHAO_TUYA);
    }


    return 0;
}

static void __tkl_kws_task(void *args)
{
    uint8_t *mic_data = NULL;

	// OPERATE_RET rt = 0;
    for (;;) {
        // read mic data from ringbuf
        uint32_t rb_data_len = tuya_ring_buff_used_size_get(g_kws_ctx.mic_ringbuf);
        if (rb_data_len < MODEL_CHUNK_SIZE) {
            tkl_system_sleep(10);
            continue;;
        }

        if (mic_data == NULL) {
            mic_data = (uint8_t *)tkl_system_malloc(MODEL_CHUNK_SIZE);
            if (mic_data == NULL) {
                tkl_system_sleep(10);
                continue;;
            }
        }

        memset(mic_data, 0, MODEL_CHUNK_SIZE);
        tuya_ring_buff_read(g_kws_ctx.mic_ringbuf, mic_data, MODEL_CHUNK_SIZE);

        g_kws_ctx.module->process(g_kws_ctx.module->self, (char *)mic_data, MODEL_CHUNK_SIZE, NULL, MODEL_CHUNK_SIZE);

        tkl_system_sleep(10);
    }

}

/* [PATCH-SHERPA-WAKEUP] 外部唤醒监听线程
 * 监听 Unix socket，任何进程 connect 一下（sherpa 检测到「你好小鸭」时），
 * 就等价于内置 KWS 命中：调用已注册的 wakeup_cb 触发主程序完整对话链路
 * （播提示音 -> 切 LISTEN -> VAD 检测人声 -> 云端 ASR -> LLM -> TTS）。
 */
static void __tkl_kws_external_wakeup_task(void *args)
{
    int listen_fd = -1;

    listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        printf("[kws-ext] socket create failed: %s\n", strerror(errno));
        return;
    }

    unlink(EXTERNAL_WAKEUP_SOCK);

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", EXTERNAL_WAKEUP_SOCK);

    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        printf("[kws-ext] bind %s failed: %s\n", EXTERNAL_WAKEUP_SOCK, strerror(errno));
        close(listen_fd);
        return;
    }

    if (listen(listen_fd, 4) < 0) {
        printf("[kws-ext] listen failed: %s\n", strerror(errno));
        close(listen_fd);
        return;
    }

    chmod(EXTERNAL_WAKEUP_SOCK, 0666);

    g_kws_ctx.ext_wakeup_fd = listen_fd;
    printf("[kws-ext] external wakeup socket listening on %s\n", EXTERNAL_WAKEUP_SOCK);

    char buf[64];
    for (;;) {
        int conn = accept(listen_fd, NULL, NULL);
        if (conn < 0) {
            if (errno == EINTR) {
                continue;
            }
            printf("[kws-ext] accept failed: %s\n", strerror(errno));
            tkl_system_sleep(100);
            continue;
        }

        // 读（可选）以确认客户端已连接，但内容不重要，收到连接即视为唤醒信号
        ssize_t n = recv(conn, buf, sizeof(buf), 0);
        (void)n;

        close(conn);

        printf("[kws-ext] wakeup signal received, triggering wakeup_cb\n");

        if (g_kws_ctx.wakeup_cb != NULL) {
            g_kws_ctx.wakeup_cb(TKL_KWS_WAKEUP_NIHAO_TUYA);
        }
    }
}

/* [PATCH-SHERPA-WAKEUP] 麦克风音频转发线程
 * 监听 AUDIO_FEED_SOCK，sherpa 进程连上后，主程序把 __tkl_kws_process 收到的
 * PCM 流（16k/mono/S16_LE）持续转发给它，供 sherpa 做唤醒词识别。
 * 避免 sherpa 自己 arecord 与主程序争抢 plughw:0,0（声卡独占）。
 */
static void __tkl_kws_audio_feed_task(void *args)
{
    int listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        printf("[kws-feed] socket create failed: %s\n", strerror(errno));
        return;
    }

    unlink(AUDIO_FEED_SOCK);

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", AUDIO_FEED_SOCK);

    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        printf("[kws-feed] bind %s failed: %s\n", AUDIO_FEED_SOCK, strerror(errno));
        close(listen_fd);
        return;
    }

    if (listen(listen_fd, 4) < 0) {
        printf("[kws-feed] listen failed: %s\n", strerror(errno));
        close(listen_fd);
        return;
    }

    chmod(AUDIO_FEED_SOCK, 0666);
    printf("[kws-feed] audio feed socket listening on %s\n", AUDIO_FEED_SOCK);

    for (;;) {
        int conn = accept(listen_fd, NULL, NULL);
        if (conn < 0) {
            if (errno == EINTR) {
                continue;
            }
            printf("[kws-feed] accept failed: %s\n", strerror(errno));
            tkl_system_sleep(100);
            continue;
        }

        /* 新客户端连接，替换旧的（同一时刻只服务一个 sherpa） */
        int flags = fcntl(conn, F_GETFL, 0);
        if (flags >= 0) {
            fcntl(conn, F_SETFL, flags | O_NONBLOCK);
        }
        if (g_kws_ctx.audio_feed_fd > 0) {
            close(g_kws_ctx.audio_feed_fd);
        }
        g_kws_ctx.audio_feed_fd = conn;
        printf("[kws-feed] sherpa audio client connected (fd=%d)\n", conn);
    }
}



OPERATE_RET tkl_kws_init(void)
{
    OPERATE_RET rt = OPRT_OK;

    const char *modelpath_cfg = NULL;
    const char *tokenpath_cfg = NULL;
    static char modelpath[512] = {0};
    static char tokenpath[512] = {0};

#ifdef KWS_MODEL_PATH
    modelpath_cfg = KWS_MODEL_PATH;
#endif
#ifdef KWS_MODEL_TOKEN_PATH
    tokenpath_cfg = KWS_MODEL_TOKEN_PATH;
#endif

    if (modelpath_cfg == NULL || strlen(modelpath_cfg) == 0) {
        modelpath_cfg = "~/tuyaopen_models/mdtc_chunk_300ms.mnn";
    }
    if (tokenpath_cfg == NULL || strlen(tokenpath_cfg) == 0) {
        tokenpath_cfg = "~/tuyaopen_models/tokens.txt";
    }

    __expand_user_path(modelpath_cfg, modelpath, sizeof(modelpath));
    __expand_user_path(tokenpath_cfg, tokenpath, sizeof(tokenpath));

    // ringbuf create for mic data
    rt = tuya_ring_buff_create(KWS_RING_BUFF_LEN, OVERFLOW_STOP_TYPE, &g_kws_ctx.mic_ringbuf);
    if (rt != OPRT_OK) {
        printf("kws mic ringbuff create failed\n");
        return rt;
    }

    char keyword[] = "你好涂鸦";

    kws_factory2_config_t kwsconfig;
    kwsconfig.interpreter_type = 2; // mnn  
    kwsconfig.chunk_size = 300 * 16; // 300ms, 16k
    kwsconfig.feats_size = 30;
    kwsconfig.sample_rate = 16000;
    kwsconfig.interval = 100;
    kwsconfig.model_type = 0;
    kwsconfig.num_bins = 80;
    kwsconfig.batch_size = 10;
    kwsconfig.threshold = 0.8;
    kwsconfig.key_word = keyword;
    kwsconfig.model_path = modelpath;
    kwsconfig.token_path = tokenpath;

    int err = 0;
    g_kws_ctx.module = &audio_subsys_kws_factory2;
    int ret = g_kws_ctx.module->create(&g_kws_ctx.module->self, &kwsconfig);
    if (ret < 0) {
        printf("kws module create failed\n");
        return -1;
    }

    err = g_kws_ctx.module->set_callback(g_kws_ctx.module->self, __tkl_kws_result, NULL);
    if (err != 0) {
        printf("kws module set callback failed\n");
        tkl_kws_deinit();
        return -1;
    }

    tkl_thread_create(&g_kws_ctx.kws_thread,
                        "tkl_kws_task",
                        4096,
                        4,
                        __tkl_kws_task,
                        NULL);

    /* [PATCH-SHERPA-WAKEUP] 启动外部唤醒监听线程 */
    tkl_thread_create(&g_kws_ctx.ext_wakeup_thread,
                        "tkl_kws_ext_wk",
                        4096,
                        4,
                        __tkl_kws_external_wakeup_task,
                        NULL);

    /* [PATCH-SHERPA-WAKEUP] 启动麦克风音频转发线程 */
    g_kws_ctx.audio_feed_fd = -1;
    tkl_thread_create(&g_kws_ctx.audio_feed_thread,
                        "tkl_kws_aud_feed",
                        4096,
                        4,
                        __tkl_kws_audio_feed_task,
                        NULL);

#if defined(ENABLE_AUDIO_ALSA) && (ENABLE_AUDIO_ALSA == 1)
    tdd_audio_alsa_register_kws_feed_cb(__tkl_kws_process);
#endif

    return rt;
}

OPERATE_RET tkl_kws_enable(void)
{
    return OPRT_OK;
}

OPERATE_RET tkl_kws_disable(void)
{
    return OPRT_OK;
}

OPERATE_RET tkl_kws_reg_wakeup_cb(TKL_KWS_WAKEUP_CB wakeup_cb)
{
    g_kws_ctx.wakeup_cb = wakeup_cb;
    return OPRT_OK;
}

OPERATE_RET tkl_kws_deinit(void)
{
    /* [PATCH-SHERPA-WAKEUP] 关闭外部唤醒 socket */
    if (g_kws_ctx.ext_wakeup_fd > 0) {
        close(g_kws_ctx.ext_wakeup_fd);
        g_kws_ctx.ext_wakeup_fd = -1;
        unlink(EXTERNAL_WAKEUP_SOCK);
    }
    if (g_kws_ctx.ext_wakeup_thread != NULL) {
        tkl_thread_release(g_kws_ctx.ext_wakeup_thread);
        g_kws_ctx.ext_wakeup_thread = NULL;
    }

    /* [PATCH-SHERPA-WAKEUP] 关闭音频转发 socket */
    if (g_kws_ctx.audio_feed_fd > 0) {
        close(g_kws_ctx.audio_feed_fd);
        g_kws_ctx.audio_feed_fd = -1;
    }
    unlink(AUDIO_FEED_SOCK);
    if (g_kws_ctx.audio_feed_thread != NULL) {
        tkl_thread_release(g_kws_ctx.audio_feed_thread);
        g_kws_ctx.audio_feed_thread = NULL;
    }

    if (g_kws_ctx.module == NULL) {
        return OPRT_OK;
    }

    if (g_kws_ctx.kws_thread != NULL) {
        tuya_ring_buff_free(g_kws_ctx.kws_thread);
    }

    tkl_thread_release(g_kws_ctx.kws_thread);

    g_kws_ctx.module->destroy(g_kws_ctx.module->self);

    g_kws_ctx.module = NULL;

    return OPRT_OK;
}


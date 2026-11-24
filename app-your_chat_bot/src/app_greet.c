/**
 * @file app_greet.c
 * @brief Greet mode TTS trigger for DshanPi A1 lamp robot
 *
 * [PATCH-LAMP-MCP] 迎宾模式语音招呼：
 * - greet_service.py（摄像头人脸检测）检测到新客人 → 写 /run/greet_hi
 * - 本模块 500ms 轮询该文件，读到后删除并调用 ai_agent_send_text()
 *   发打招呼文本，云端 Agent 返回 TTS 音频自动播放
 * - 双层节流：脚本侧 45s 冷却 + 本模块 30s 保底，避免重复轰炸
 *
 * @copyright Copyright (c) 2021-2025 Tuya Inc. All Rights Reserved.
 */

#include "tal_api.h"
#include "tkl_thread.h"

#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <string.h>

#include "tuya_ai_agent.h"

#include "app_greet.h"
#include "app_led_ring.h"

/***********************************************************
************************macro define************************
***********************************************************/
#define GREET_HI_FILE       "/run/greet_hi"
#define GREET_BYE_FILE      "/run/greet_bye"
#define GREET_POLL_MS       500     /* 轮询周期 */
#define GREET_MIN_GAP_MS    (30 * 1000)  /* 保底节流：两次招呼最小间隔 */

/* [PATCH-LAMP-MCP] 迎宾招呼：让云端原样重复这段固定话术（保证 TTS 内容可控） */
#define GREET_TEXT  "重复这句话：嗨，你好呀！很高兴认识你！我是这盏会动的台灯小鸭。" \
                    "想和我玩的话，对着我说\"你好小鸭\"唤醒我，" \
                    "我就能陪你聊天、讲故事，还可以帮你开关灯、变换灯光颜色哦！"

/***********************************************************
***********************variable define**********************
***********************************************************/
static THREAD_HANDLE sg_thread = NULL;
static volatile uint8_t sg_running = 0;
static volatile BOOL_T sg_inited = FALSE;
static uint64_t sg_last_hi_ms = 0;

/***********************************************************
***********************function define**********************
***********************************************************/
static uint64_t __now_ms(void)
{
    return (uint64_t)tal_system_get_millisecond();
}

static BOOL_T __file_exists(const char *path)
{
    struct stat st;
    return (stat(path, &st) == 0 && S_ISREG(st.st_mode)) ? TRUE : FALSE;
}

static void __greet_task(void *arg)
{
    char buf[64] = {0};
    PR_DEBUG("greet trigger task start");

    while (sg_running) {
        /* 客人离开：关灯（暗待机） */
        if (__file_exists(GREET_BYE_FILE)) {
            unlink(GREET_BYE_FILE);
            app_led_ring_set_effect(APP_LED_EFFECT_NONE);
            app_led_ring_set_power(FALSE);
            PR_DEBUG("greet: guest left, LED ring off");
        }

        /* 新客人：彩虹呼吸灯效 + 固定话术 TTS */
        if (__file_exists(GREET_HI_FILE)) {
            /* 读内容（区分触发类型），然后立即删除避免重复 */
            int fd = open(GREET_HI_FILE, O_RDONLY);
            if (fd >= 0) {
                ssize_t n = read(fd, buf, sizeof(buf) - 1);
                if (n > 0) {
                    buf[n] = '\0';
                }
                close(fd);
            }
            unlink(GREET_HI_FILE);

            uint64_t now = __now_ms();
            if (sg_last_hi_ms != 0 && now - sg_last_hi_ms < GREET_MIN_GAP_MS) {
                PR_DEBUG("greet: suppressed by %dms guard", GREET_MIN_GAP_MS);
            } else {
                sg_last_hi_ms = now;
                PR_NOTICE("greet: guest detected (%s), rainbow LED + TTS hello", buf);

                /* 迎宾灯效：彩虹色 + 10% 亮度 */
                app_led_ring_init();
                app_led_ring_set_power(TRUE);
                app_led_ring_set_brightness(10);
                app_led_ring_set_effect(APP_LED_EFFECT_RAINBOW);

                /* 固定话术：让云端原样重复 */
                OPERATE_RET rt = ai_agent_send_text((char *)GREET_TEXT);
                if (OPRT_OK != rt) {
                    PR_ERR("greet: ai_agent_send_text failed rt=%d", rt);
                }
            }
        }
        tal_system_sleep(GREET_POLL_MS);
    }

    PR_DEBUG("greet trigger task exit");
}

OPERATE_RET app_greet_init(void)
{
    if (sg_inited) {
        return OPRT_OK;
    }

    sg_running = 1;
    THREAD_CFG_T thrd_cfg = {
        .priority = THREAD_PRIO_6,
        .stackDepth = 2 * 1024,
        .thrdname = "greet_hi",
        .psram_mode = 0,
    };
    OPERATE_RET rt = tal_thread_create_and_start(&sg_thread, NULL, NULL, __greet_task, NULL, &thrd_cfg);
    if (OPRT_OK != rt) {
        sg_running = 0;
        return rt;
    }

    sg_inited = TRUE;
    return OPRT_OK;
}

/**
 * @file app_led_ring.c
 * @brief WS2812 LED ring effect engine for DshanPi A1 lamp robot
 *
 * [PATCH-LAMP-MCP]
 * - 设备：/dev/ws2812-leds（SPI2 + ws2812_drv.ko，24 颗灯，GRB 字节序，3 字节/灯）
 * - 驱动特性：close(fd) 会自动熄灭所有灯，因此引擎在整个进程生命周期持有 fd
 * - 渲染线程按 ~20ms 一帧写帧；关灯时只写一次全零帧然后休眠，不占用 SPI
 *
 * @copyright Copyright (c) 2021-2025 Tuya Inc. All Rights Reserved.
 */

#include "tal_api.h"
#include "tkl_thread.h"
#include "tkl_mutex.h"

#include <fcntl.h>
#include <unistd.h>
#include <string.h>

#include "app_led_ring.h"

/***********************************************************
************************macro define************************
***********************************************************/
#define LED_RING_DEV        "/dev/ws2812-leds"
#define LED_RING_NUM        24      /* 设备树 leds,number=24 */
#define LED_TICK_MS         20      /* 渲染帧间隔 */
#define LED_BRIGHT_DEFAULT  80

/***********************************************************
***********************typedef define***********************
***********************************************************/
/* WS2812 GRB 字节序 */
typedef struct {
    uint8_t g;
    uint8_t r;
    uint8_t b;
} LED_GRB_T;

typedef struct {
    BOOL_T   power;
    uint8_t  r, g, b;        /* 目标颜色 0-255 */
    uint8_t  brightness;     /* 0-100 */
    int      effect;         /* APP_LED_EFFECT_E */
    uint8_t  speed;          /* 1-10 */
} LED_PARAM_T;

/***********************************************************
***********************variable define**********************
***********************************************************/
static MUTEX_HANDLE sg_mutex = NULL;
static THREAD_HANDLE sg_thread = NULL;
static int sg_fd = -1;
static volatile uint8_t sg_running = 0;
static volatile BOOL_T sg_inited = FALSE;

static LED_PARAM_T sg_param = {
    .power = FALSE,          /* 开机默认灭灯，等用户语音控制 */
    .r = 255, .g = 255, .b = 255,
    .brightness = LED_BRIGHT_DEFAULT,
    .effect = APP_LED_EFFECT_STATIC,
    .speed = 5,
};

/***********************************************************
***********************function define**********************
***********************************************************/
static void __params_get(LED_PARAM_T *p)
{
    tal_mutex_lock(sg_mutex);
    *p = sg_param;
    tal_mutex_unlock(sg_mutex);
}

static void __frame_send(const LED_GRB_T *frame, int num)
{
    if (sg_fd < 0) {
        return;
    }
    ssize_t n = write(sg_fd, frame, num * sizeof(LED_GRB_T));
    (void)n;
}

/* h: 0-359, s/v: 0-255 */
static void __hsv_to_rgb(uint16_t h, uint8_t s, uint8_t v, uint8_t *r, uint8_t *g, uint8_t *b)
{
    uint8_t region = h / 60;
    uint16_t rem = (h % 60) * 255 / 60;
    uint8_t p = (v * (255 - s)) / 255;
    uint8_t q = (v * (255 - (uint16_t)s * rem / 255)) / 255;
    uint8_t t = (v * (255 - (uint16_t)s * (255 - rem) / 255)) / 255;

    switch (region) {
    case 0: *r = v; *g = t; *b = p; break;
    case 1: *r = q; *g = v; *b = p; break;
    case 2: *r = p; *g = v; *b = t; break;
    case 3: *r = p; *g = q; *b = v; break;
    case 4: *r = t; *g = p; *b = v; break;
    default: *r = v; *g = p; *b = q; break;
    }
}

static void __render_frame(LED_GRB_T *frame, int num, LED_PARAM_T *p, uint32_t t_ms)
{
    int i;
    uint32_t scale = (uint32_t)p->brightness * 255 / 100; /* 0-255 */

    switch (p->effect) {
    case APP_LED_EFFECT_STATIC: {
        uint8_t r = (uint8_t)((uint16_t)p->r * scale / 255);
        uint8_t g = (uint8_t)((uint16_t)p->g * scale / 255);
        uint8_t b = (uint8_t)((uint16_t)p->b * scale / 255);
        for (i = 0; i < num; i++) {
            frame[i].r = r;
            frame[i].g = g;
            frame[i].b = b;
        }
    } break;

    case APP_LED_EFFECT_BREATH: {
        /* 三角波亮度：speed 5 时一个呼吸周期约 2.4s（6000/speed ms） */
        uint32_t speed = p->speed ? p->speed : 5;
        uint32_t period = 6000 / speed;
        uint32_t half = period / 2;
        uint32_t ph = t_ms % period;
        uint32_t wave = (ph < half) ? (ph * 255 / half)
                                    : (255 - (ph - half) * 255 / (period - half));
        uint32_t s = wave * scale / 255;
        uint8_t r = (uint8_t)((uint16_t)p->r * s / 255);
        uint8_t g = (uint8_t)((uint16_t)p->g * s / 255);
        uint8_t b = (uint8_t)((uint16_t)p->b * s / 255);
        for (i = 0; i < num; i++) {
            frame[i].r = r;
            frame[i].g = g;
            frame[i].b = b;
        }
    } break;

    case APP_LED_EFFECT_RUNNING: {
        /* 带 4 灯拖尾的流水点，speed 5 时约 0.8s 绕一圈（4000/speed ms） */
        uint32_t speed = p->speed ? p->speed : 5;
        uint32_t period = 4000 / speed;
        const int tail = 4;
        int pos = (int)((t_ms % period) * num / period);
        int k;

        memset(frame, 0, num * sizeof(LED_GRB_T));
        for (k = 0; k < tail; k++) {
            int idx = ((pos - k) % num + num) % num;
            uint32_t f = (uint32_t)(tail - k) * 255 / tail; /* 拖尾衰减 */
            frame[idx].r = (uint8_t)((uint16_t)p->r * f * scale / (255 * 255));
            frame[idx].g = (uint8_t)((uint16_t)p->g * f * scale / (255 * 255));
            frame[idx].b = (uint8_t)((uint16_t)p->b * f * scale / (255 * 255));
        }
    } break;

    case APP_LED_EFFECT_RAINBOW: {
        /* 彩虹流转：整环色相平移，speed 5 时约 0.8s 转一圈 */
        uint32_t speed = p->speed ? p->speed : 5;
        uint32_t period = 4000 / speed;
        uint32_t base = (t_ms % period) * 360 / period;
        for (i = 0; i < num; i++) {
            uint16_t hue = (base + i * 360 / num) % 360;
            uint8_t r, g, b;
            __hsv_to_rgb(hue, 255, (uint8_t)scale, &r, &g, &b);
            frame[i].r = r;
            frame[i].g = g;
            frame[i].b = b;
        }
    } break;

    case APP_LED_EFFECT_RAINBOW_BREATH: {
        /* 彩虹色相缓慢流转 + 亮度呼吸叠加（呼吸深度 50%~100% 亮度），
         * 迎宾模式检测到客人时的专属灯效 */
        uint32_t speed = p->speed ? p->speed : 5;
        uint32_t hue_period = 6000 / speed;    /* 色相流转周期 */
        uint32_t breath_period = 4000 / speed; /* 呼吸周期 */
        uint32_t base_hue = (t_ms % hue_period) * 360 / hue_period;
        uint32_t bph = t_ms % breath_period;
        uint32_t bhalf = breath_period / 2;
        uint32_t wave = (bph < bhalf) ? (bph * 255 / bhalf)
                                      : (255 - (bph - bhalf) * 255 / (breath_period - bhalf));
        uint32_t v = scale * (255 + wave) / (2 * 255); /* scale 的 50%~100% */
        int i;
        for (i = 0; i < num; i++) {
            uint16_t hue = (base_hue + i * 360 / num) % 360;
            uint8_t r, g, b;
            __hsv_to_rgb(hue, 255, (uint8_t)v, &r, &g, &b);
            frame[i].r = r;
            frame[i].g = g;
            frame[i].b = b;
        }
    } break;

    default: {
        memset(frame, 0, num * sizeof(LED_GRB_T));
    } break;
    }
}

static void __led_task(void *arg)
{
    LED_GRB_T frame[LED_RING_NUM];
    LED_PARAM_T p;
    uint32_t tick = 0;
    BOOL_T cleared = TRUE;   /* 初始态为灭 */

    PR_DEBUG("led ring task start, num=%d", LED_RING_NUM);

    while (sg_running) {
        __params_get(&p);

        if (!p.power || p.effect == APP_LED_EFFECT_NONE) {
            if (!cleared) {
                memset(frame, 0, sizeof(frame));
                __frame_send(frame, LED_RING_NUM);
                cleared = TRUE;
            }
            tal_system_sleep(100);
            continue;
        }

        cleared = FALSE;
        __render_frame(frame, LED_RING_NUM, &p, tick * LED_TICK_MS);
        __frame_send(frame, LED_RING_NUM);
        tick++;
        tal_system_sleep(LED_TICK_MS);
    }

    /* 线程退出：发一帧全零，保证熄灭 */
    memset(frame, 0, sizeof(frame));
    __frame_send(frame, LED_RING_NUM);
    PR_DEBUG("led ring task exit");
}

OPERATE_RET app_led_ring_init(void)
{
    OPERATE_RET rt = OPRT_OK;

    if (sg_inited) {
        return OPRT_OK;
    }

    TUYA_CALL_ERR_LOG(tal_mutex_create_init(&sg_mutex));

    sg_fd = open(LED_RING_DEV, O_WRONLY);
    if (sg_fd < 0) {
        PR_ERR("open %s failed, check ws2812_drv module", LED_RING_DEV);
        return OPRT_COM_ERROR;
    }

    sg_running = 1;
    THREAD_CFG_T thrd_cfg = {
        .priority = THREAD_PRIO_6,
        .stackDepth = 2 * 1024,
        .thrdname = "led_ring",
        .psram_mode = 0,
    };
    rt = tal_thread_create_and_start(&sg_thread, NULL, NULL, __led_task, NULL, &thrd_cfg);
    if (OPRT_OK != rt) {
        sg_running = 0;
        close(sg_fd);
        sg_fd = -1;
        return rt;
    }

    sg_inited = TRUE;
    PR_DEBUG("led ring engine initialized");

    return OPRT_OK;
}

OPERATE_RET app_led_ring_set_power(BOOL_T on)
{
    tal_mutex_lock(sg_mutex);
    sg_param.power = on;
    tal_mutex_unlock(sg_mutex);
    return OPRT_OK;
}

OPERATE_RET app_led_ring_set_color(uint8_t r, uint8_t g, uint8_t b)
{
    tal_mutex_lock(sg_mutex);
    sg_param.r = r;
    sg_param.g = g;
    sg_param.b = b;
    tal_mutex_unlock(sg_mutex);
    return OPRT_OK;
}

OPERATE_RET app_led_ring_get_color(uint8_t *r, uint8_t *g, uint8_t *b)
{
    LED_PARAM_T p;
    __params_get(&p);
    if (r) *r = p.r;
    if (g) *g = p.g;
    if (b) *b = p.b;
    return OPRT_OK;
}

OPERATE_RET app_led_ring_set_brightness(uint8_t brightness)
{
    tal_mutex_lock(sg_mutex);
    sg_param.brightness = (brightness > 100) ? 100 : brightness;
    tal_mutex_unlock(sg_mutex);
    return OPRT_OK;
}

OPERATE_RET app_led_ring_set_effect(int effect)
{
    if (effect < APP_LED_EFFECT_NONE || effect > APP_LED_EFFECT_RAINBOW) {
        return OPRT_INVALID_PARM;
    }
    tal_mutex_lock(sg_mutex);
    sg_param.effect = effect;
    tal_mutex_unlock(sg_mutex);
    return OPRT_OK;
}

OPERATE_RET app_led_ring_set_speed(uint8_t speed)
{
    if (speed < 1 || speed > 10) {
        return OPRT_INVALID_PARM;
    }
    tal_mutex_lock(sg_mutex);
    sg_param.speed = speed;
    tal_mutex_unlock(sg_mutex);
    return OPRT_OK;
}

/* [PATCH-LAMP-MCP] 读取引擎当前完整状态（供 MCP 工具回显给云端大模型） */
OPERATE_RET app_led_ring_get_state(BOOL_T *on, int *effect, uint8_t *brightness, uint8_t *speed)
{
    LED_PARAM_T p;
    __params_get(&p);
    if (on) *on = p.power;
    if (effect) *effect = p.effect;
    if (brightness) *brightness = p.brightness;
    if (speed) *speed = p.speed;
    return OPRT_OK;
}

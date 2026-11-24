/**
 * @file app_led_ring.h
 * @brief WS2812 LED ring effect engine for DshanPi A1 lamp robot
 *
 * [PATCH-LAMP-MCP] 通过 MCP 工具控制灯环：开关灯、颜色、亮度、
 * 静态/呼吸/流水/彩虹灯效。引擎持有 /dev/ws2812-leds 的 fd，
 * 由独立线程按 ~20ms 一帧持续渲染。进程退出关闭 fd 时驱动自动熄灭所有灯。
 *
 * @copyright Copyright (c) 2021-2025 Tuya Inc. All Rights Reserved.
 */

#ifndef __APP_LED_RING_H__
#define __APP_LED_RING_H__

#include "tuya_cloud_types.h"

typedef enum {
    APP_LED_EFFECT_NONE = 0,    /* 全灭（等价关灯） */
    APP_LED_EFFECT_STATIC = 1,  /* 纯色常亮 */
    APP_LED_EFFECT_BREATH = 2,  /* 呼吸 */
    APP_LED_EFFECT_RUNNING = 3, /* 流水 */
    APP_LED_EFFECT_RAINBOW = 4, /* 彩虹流转 */
    APP_LED_EFFECT_RAINBOW_BREATH = 5, /* 彩虹色相流转 + 亮度呼吸叠加 */
} APP_LED_EFFECT_E;

/**
 * @brief 初始化灯环引擎（幂等）：打开设备节点并启动渲染线程
 */
OPERATE_RET app_led_ring_init(void);

/**
 * @brief 开/关灯（关灯 = 全灭但引擎线程继续运行）
 */
OPERATE_RET app_led_ring_set_power(BOOL_T on);

/**
 * @brief 设置纯色（RGB 0-255，影响 static/breath/running 的颜色）
 */
OPERATE_RET app_led_ring_set_color(uint8_t r, uint8_t g, uint8_t b);

/**
 * @brief 读取当前颜色
 */
OPERATE_RET app_led_ring_get_color(uint8_t *r, uint8_t *g, uint8_t *b);

/**
 * @brief 设置亮度 0-100（所有灯效统一缩放）
 */
OPERATE_RET app_led_ring_set_brightness(uint8_t brightness);

/**
 * @brief 设置灯效（APP_LED_EFFECT_E）
 */
OPERATE_RET app_led_ring_set_effect(int effect);

/**
 * @brief 设置灯效速度 1-10（1 最慢，10 最快）
 */
OPERATE_RET app_led_ring_set_speed(uint8_t speed);

#endif /* __APP_LED_RING_H__ */

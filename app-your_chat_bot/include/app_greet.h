/**
 * @file app_greet.h
 * @brief Greet mode TTS trigger: watches /run/greet_hi written by greet_service.py
 *
 * [PATCH-LAMP-MCP] 迎宾模式：greet_service.py 检测到新客人时写 /run/greet_hi，
 * 本模块轮询到该文件后调用 ai_agent_send_text() 发一段打招呼文本，
 * 由云端返回 TTS 播放。
 *
 * @copyright Copyright (c) 2021-2025 Tuya Inc. All Rights Reserved.
 */

#ifndef __APP_GREET_H__
#define __APP_GREET_H__

#include "tuya_cloud_types.h"

/**
 * @brief 启动迎宾 TTS 触发监听线程（幂等，500ms 轮询 + 30s 保底节流）
 */
OPERATE_RET app_greet_init(void);

#endif /* __APP_GREET_H__ */

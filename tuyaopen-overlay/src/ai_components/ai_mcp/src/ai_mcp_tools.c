/**
 * @file ai_mcp_tools.c
 * @version 0.1
 * @copyright Copyright (c) 2021-2025 Tuya Inc. All Rights Reserved.
 *
 */

#include "tal_api.h"

#include "tuya_ai_agent.h"

#include "ai_manage_mode.h"

#if defined(ENABLE_COMP_AI_AUDIO) && (ENABLE_COMP_AI_AUDIO == 1)
#include "ai_audio_player.h"
#endif

#if defined(ENABLE_COMP_AI_VIDEO) && (ENABLE_COMP_AI_VIDEO == 1)
#include "ai_video_input.h"
#endif

#if defined(ENABLE_COMP_AI_PICTURE) && (ENABLE_COMP_AI_PICTURE == 1)
#include "ai_picture.h"
#endif

#if defined(ENABLE_COMP_AI_DISPLAY) && (ENABLE_COMP_AI_DISPLAY == 1)
#include "ai_ui_manage.h"
#endif

#include "ai_mcp_server.h"

#include "ai_mcp.h"

/* [PATCH-LAMP-MCP] 灯环引擎实现在 app 侧 app_led_ring.c（aux_source_directory 收集），
 * 这里用 extern 声明直接链接，避免 SDK 组件依赖 app 头文件 */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
extern OPERATE_RET app_led_ring_init(void);
extern OPERATE_RET app_led_ring_set_power(BOOL_T on);
extern OPERATE_RET app_led_ring_set_color(uint8_t r, uint8_t g, uint8_t b);
extern OPERATE_RET app_led_ring_get_color(uint8_t *r, uint8_t *g, uint8_t *b);
extern OPERATE_RET app_led_ring_set_brightness(uint8_t brightness);
extern OPERATE_RET app_led_ring_set_effect(int effect);
extern OPERATE_RET app_led_ring_set_speed(uint8_t speed);
extern OPERATE_RET app_led_ring_get_state(BOOL_T *on, int *effect, uint8_t *brightness, uint8_t *speed);

/* 与 app_led_ring.h 中 APP_LED_EFFECT_E 保持一致 */
#define APP_LED_EFFECT_NONE    0
#define APP_LED_EFFECT_STATIC  1
#define APP_LED_EFFECT_BREATH  2
#define APP_LED_EFFECT_RUNNING 3
#define APP_LED_EFFECT_RAINBOW 4
#define APP_LED_EFFECT_RAINBOW_BREATH 5



/***********************************************************
***********************typedef define***********************
***********************************************************/


/***********************************************************
***********************variable define**********************
***********************************************************/


/***********************************************************
***********************function define**********************
***********************************************************/
static OPERATE_RET __get_device_info(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    cJSON *json = NULL;

    json = cJSON_CreateObject();
    if (!json) {
        PR_ERR("Create JSON object failed");
        return OPRT_MALLOC_FAILED;
    }

    // Implement device info retrieval logic here
    // Add device information
    cJSON_AddStringToObject(json, "model", PROJECT_NAME);
    cJSON_AddStringToObject(json, "serialNumber", "123456789");
    cJSON_AddStringToObject(json, "firmwareVersion", PROJECT_VERSION);

    // Set return value
    ai_mcp_return_value_set_json(ret_val, json);

    return OPRT_OK;
}
#if defined(ENABLE_COMP_AI_VIDEO) && (ENABLE_COMP_AI_VIDEO == 1)
static OPERATE_RET __take_photo(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    OPERATE_RET rt = OPRT_OK;
    uint8_t *image_data = NULL;
    uint32_t image_size = 0;

    TUYA_CALL_ERR_LOG(ai_video_start());

    tal_system_sleep(3000);

    rt = ai_video_get_jpeg_frame(&image_data, &image_size);
    if (OPRT_OK != rt) {
        PR_ERR("get jpeg frame err, rt:%d", rt);
        return rt;
    }

#if defined(ENABLE_COMP_AI_PICTURE) && (ENABLE_COMP_AI_PICTURE == 1)
    char name[AI_PICTURE_NAME_MAX_LEN + 1] = {0};
    ai_picture_save_to_album(image_data, image_size, NULL, name);

#if defined(ENABLE_COMP_AI_DISPLAY) && (ENABLE_COMP_AI_DISPLAY == 1)
    ai_ui_disp_msg(AI_UI_DISP_USER_IMAGE_LINK, (uint8_t *)name, strlen(name)+1);
#endif

#endif

    rt = ai_mcp_return_value_set_image(ret_val, MCP_IMAGE_MIME_TYPE_JPEG, image_data, image_size);
    if (OPRT_OK != rt) {
        PR_ERR("set return image err, rt:%d", rt);
        ai_video_jpeg_image_free(&image_data);
        return rt;
    }

    ai_video_jpeg_image_free(&image_data);

    TUYA_CALL_ERR_LOG(ai_video_stop());

    return OPRT_OK;
}
#endif

#if defined(ENABLE_COMP_AI_AUDIO) && (ENABLE_COMP_AI_AUDIO == 1)
static OPERATE_RET __set_volume(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    uint32_t volume = 50; // default volume

    PR_DEBUG("__set_volume enter");

    // Parse properties to get volume
    for (int i = 0; i < properties->count; i++) {
        MCP_PROPERTY_T *prop = properties->properties[i];
        if (strcmp(prop->name, "volume") == 0 && prop->type == MCP_PROPERTY_TYPE_INTEGER) {
            volume = prop->default_val.int_val;
            break;
        }
    }

    // FIXME: Implement actual volume setting logic here
    ai_audio_player_set_vol(volume);
    PR_DEBUG("set volume to %d", volume);

    // Set return value
    ai_mcp_return_value_set_bool(ret_val, TRUE);

    PR_DEBUG("__set_volume exit");

    return OPRT_OK;
}
#endif
/* [PATCH-LAMP-MCP] 属性读取辅助：properties 列表只包含模型实际传入的属性 */
static const char *__prop_get_str(const MCP_PROPERTY_LIST_T *properties, const char *name)
{
    int i;
    for (i = 0; i < properties->count; i++) {
        MCP_PROPERTY_T *prop = properties->properties[i];
        if (prop && strcmp(prop->name, name) == 0 && prop->type == MCP_PROPERTY_TYPE_STRING) {
            return prop->default_val.str_val;
        }
    }
    return NULL;
}

static BOOL_T __prop_get_int(const MCP_PROPERTY_LIST_T *properties, const char *name, int *out)
{
    int i;
    for (i = 0; i < properties->count; i++) {
        MCP_PROPERTY_T *prop = properties->properties[i];
        if (prop && strcmp(prop->name, name) == 0 && prop->type == MCP_PROPERTY_TYPE_INTEGER) {
            *out = prop->default_val.int_val;
            return TRUE;
        }
    }
    return FALSE;
}

/* [PATCH-LAMP-MCP] LED 灯环控制：开关灯 / 颜色 / 亮度 / 灯效（静态、呼吸、流水、彩虹）/ 速度 */
static OPERATE_RET __led_control(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    const char *power = NULL;
    const char *effect = NULL;
    int ival = 0;
    BOOL_T has_r, has_g, has_b;
    int r = 255, g = 255, b = 255;
    uint8_t cur_r = 255, cur_g = 255, cur_b = 255;
    char color_hex[8] = "#FFFFFF";
    char effect_str[16] = "static";
    int brightness = 80;
    char power_str[8] = "on";

    /* 首次调用惰性初始化引擎（打开 /dev/ws2812-leds + 启动渲染线程） */
    if (OPRT_OK != app_led_ring_init()) {
        ai_mcp_return_value_set_bool(ret_val, FALSE);
        return OPRT_COM_ERROR;
    }

    power = __prop_get_str(properties, "power");
    if (power) {
        app_led_ring_set_power((strcmp(power, "on") == 0) ? TRUE : FALSE);
    }

    has_r = __prop_get_int(properties, "r", &ival);
    if (has_r) r = ival;
    has_g = __prop_get_int(properties, "g", &ival);
    if (has_g) g = ival;
    has_b = __prop_get_int(properties, "b", &ival);
    if (has_b) b = ival;
    if (has_r || has_g || has_b) {
        /* 只改传了的通道，其余保持当前值 */
        app_led_ring_get_color(&cur_r, &cur_g, &cur_b);
        if (!has_r) r = cur_r;
        if (!has_g) g = cur_g;
        if (!has_b) b = cur_b;
        app_led_ring_set_color((uint8_t)r, (uint8_t)g, (uint8_t)b);
    }

    if (__prop_get_int(properties, "brightness", &ival)) {
        app_led_ring_set_brightness((uint8_t)ival);
    }

    effect = __prop_get_str(properties, "effect");
    if (effect) {
        if (strcmp(effect, "none") == 0) {
            app_led_ring_set_effect(APP_LED_EFFECT_NONE);
        } else if (strcmp(effect, "breath") == 0) {
            app_led_ring_set_effect(APP_LED_EFFECT_BREATH);
        } else if (strcmp(effect, "running") == 0) {
            app_led_ring_set_effect(APP_LED_EFFECT_RUNNING);
        } else if (strcmp(effect, "rainbow") == 0) {
            app_led_ring_set_effect(APP_LED_EFFECT_RAINBOW);
        } else if (strcmp(effect, "rainbow_breath") == 0 || strcmp(effect, "rainbow breath") == 0) {
            app_led_ring_set_effect(APP_LED_EFFECT_RAINBOW_BREATH);
        } else {
            app_led_ring_set_effect(APP_LED_EFFECT_STATIC);
        }
    }

    if (__prop_get_int(properties, "speed", &ival)) {
        if (ival < 1) ival = 1;
        if (ival > 10) ival = 10;
        app_led_ring_set_speed((uint8_t)ival);
    }

    /* 组装当前状态返回给云端大模型 */
    app_led_ring_get_color(&cur_r, &cur_g, &cur_b);
    snprintf(color_hex, sizeof(color_hex), "#%02X%02X%02X", cur_r, cur_g, cur_b);

    {
        cJSON *json = cJSON_CreateObject();
        BOOL_T on = TRUE;
        int eff = APP_LED_EFFECT_STATIC;
        extern OPERATE_RET app_led_ring_get_state(BOOL_T *on, int *effect, uint8_t *brightness, uint8_t *speed);
        app_led_ring_get_state(&on, &eff, (uint8_t *)&brightness, (uint8_t *)&ival);

            switch (eff) {
            case APP_LED_EFFECT_NONE:    snprintf(effect_str, sizeof(effect_str), "none"); break;
            case APP_LED_EFFECT_BREATH:  snprintf(effect_str, sizeof(effect_str), "breath"); break;
            case APP_LED_EFFECT_RUNNING: snprintf(effect_str, sizeof(effect_str), "running"); break;
            case APP_LED_EFFECT_RAINBOW: snprintf(effect_str, sizeof(effect_str), "rainbow"); break;
            case APP_LED_EFFECT_RAINBOW_BREATH: snprintf(effect_str, sizeof(effect_str), "rainbow_breath"); break;
            default:                     snprintf(effect_str, sizeof(effect_str), "static"); break;
            }
        snprintf(power_str, sizeof(power_str), on ? "on" : "off");

        if (json) {
            cJSON_AddStringToObject(json, "power", power_str);
            cJSON_AddStringToObject(json, "color", color_hex);
            cJSON_AddNumberToObject(json, "brightness", brightness);
            cJSON_AddStringToObject(json, "effect", effect_str);
            cJSON_AddNumberToObject(json, "speed", ival);
            ai_mcp_return_value_set_json(ret_val, json);
        } else {
            ai_mcp_return_value_set_bool(ret_val, TRUE);
        }
    }

    PR_DEBUG("led control: power=%s color=%s effect=%s", power_str, color_hex, effect_str);
    return OPRT_OK;
}

/* [PATCH-LAMP-MCP] 人脸跟随开关：拉起/停止 face-follow.service（舵机 + 摄像头） */
static OPERATE_RET __face_follow(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    const char *enable = __prop_get_str(properties, "enable");
    int rc = -1;

    if (enable == NULL) {
        ai_mcp_return_value_set_bool(ret_val, FALSE);
        return OPRT_INVALID_PARM;
    }

    if (strcmp(enable, "on") == 0 || strcmp(enable, "start") == 0) {
        /* face-follow 依赖独占摄像头与机械臂串口；与迎宾模式互斥 */
        system("systemctl stop greet >/dev/null 2>&1");
        rc = system("systemctl start face-follow >/dev/null 2>&1");
        PR_DEBUG("face follow start rc=%d", rc);
    } else if (strcmp(enable, "off") == 0 || strcmp(enable, "stop") == 0) {
        rc = system("systemctl stop face-follow >/dev/null 2>&1");
        /* [PATCH-MANUAL] 关闭人脸跟随后自动进入手动调节模式（保持当前形状） */
        system("systemctl start arm-manual >/dev/null 2>&1");
        PR_DEBUG("face follow stop rc=%d, arm-manual started", rc);
    } else {
        ai_mcp_return_value_set_bool(ret_val, FALSE);
        return OPRT_INVALID_PARM;
    }

    ai_mcp_return_value_set_bool(ret_val, (rc == 0) ? TRUE : FALSE);
    return OPRT_OK;
}

/* [PATCH-LAMP-MCP] 迎宾模式开关：
 * on: 停人脸跟随 -> 启动 greet.service（内部自动回迎宾基准位+锁位，
 *     YuNet 检测到新客人时转灯头(5号)朝人并写 /run/greet_hi，
 *     chatbot 的 app_greet.c 监听到后调 ai_agent_send_text 让云端 TTS 打招呼）
 * off: 停 greet 服务（机械臂保持迎宾位锁住） */
static OPERATE_RET __greet_mode(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    const char *enable = __prop_get_str(properties, "enable");
    int rc = -1;

    if (enable == NULL) {
        ai_mcp_return_value_set_bool(ret_val, FALSE);
        return OPRT_INVALID_PARM;
    }

    if (strcmp(enable, "on") == 0 || strcmp(enable, "start") == 0) {
        system("systemctl stop face-follow >/dev/null 2>&1");
        rc = system("systemctl start greet >/dev/null 2>&1");
        PR_DEBUG("greet mode start rc=%d", rc);
    } else if (strcmp(enable, "off") == 0 || strcmp(enable, "stop") == 0) {
        rc = system("systemctl stop greet >/dev/null 2>&1");
        /* [PATCH-MANUAL] 退出迎宾模式后自动进入手动调节模式（保持当前形状） */
        system("systemctl start arm-manual >/dev/null 2>&1");
        PR_DEBUG("greet mode stop rc=%d, arm-manual started", rc);
    } else {
        ai_mcp_return_value_set_bool(ret_val, FALSE);
        return OPRT_INVALID_PARM;
    }

    ai_mcp_return_value_set_bool(ret_val, (rc == 0) ? TRUE : FALSE);
    return OPRT_OK;
}

/* [PATCH-LAMP-MCP] 手动调节模式开关（等效重力补偿：软跟随 + 松手锁定）：
 * on: 停 greet/face-follow -> 启动 arm-manual.service。
 *     服务内部从当前姿态锁定力矩，用户用手掰动机械臂时舵机进入软跟随
 *     （目标位实时改写为实际位置，手感轻），松手后自动锁定当前位置，
 *     位置环全力矩撑住重力 → 机械臂保持住用户摆的形状。
 * off: 停 arm-manual.service（退出时保持当前位置锁定，形状不丢）。 */
static OPERATE_RET __arm_manual(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    const char *enable = __prop_get_str(properties, "enable");
    int rc = -1;

    if (enable == NULL) {
        ai_mcp_return_value_set_bool(ret_val, FALSE);
        return OPRT_INVALID_PARM;
    }

    if (strcmp(enable, "on") == 0 || strcmp(enable, "start") == 0) {
        /* arm-manual.unit 有 Conflicts=，但显式停一遍更稳（防单元互斥被改） */
        system("systemctl stop greet >/dev/null 2>&1");
        system("systemctl stop face-follow >/dev/null 2>&1");
        rc = system("systemctl start arm-manual >/dev/null 2>&1");
        PR_DEBUG("arm manual start rc=%d", rc);
    } else if (strcmp(enable, "off") == 0 || strcmp(enable, "stop") == 0) {
        rc = system("systemctl stop arm-manual >/dev/null 2>&1");
        PR_DEBUG("arm manual stop rc=%d", rc);
    } else {
        ai_mcp_return_value_set_bool(ret_val, FALSE);
        return OPRT_INVALID_PARM;
    }

    ai_mcp_return_value_set_bool(ret_val, (rc == 0) ? TRUE : FALSE);
    return OPRT_OK;
}

/* [PATCH-LAMP-MCP] 纯卸力：全舵机释放力矩（不移动任何关节）。
 * 由 /usr/local/sbin/arm_relax.py 完成：先停掉占串口的服务再逐轴 torque off。
 * 卸力后机械臂被自重压塌属正常；「收起机械臂」是先归位再卸力的版本。 */
static OPERATE_RET __arm_relax(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    system("systemctl stop greet >/dev/null 2>&1");
    system("systemctl stop face-follow >/dev/null 2>&1");
    system("systemctl stop arm-manual >/dev/null 2>&1");
    system("systemctl stop vision-greet >/dev/null 2>&1");

    BOOL_T released = FALSE;
    FILE *fp = popen("/root/armenv/bin/python /usr/local/sbin/arm_relax.py 2>/dev/null", "r");
    if (fp) {
        char line[512];
        while (fgets(line, sizeof(line), fp)) {
            if (strstr(line, "\"released\"") != NULL) {
                released = TRUE;
            }
        }
        pclose(fp);
    }
    PR_DEBUG("arm relax: released=%d", released);

    cJSON *json = cJSON_CreateObject();
    if (json) {
        cJSON_AddBoolToObject(json, "released", released);
        cJSON_AddStringToObject(json, "note",
                                "All servo torque released, the arm is limp now.");
        ai_mcp_return_value_set_json(ret_val, json);
    } else {
        ai_mcp_return_value_set_bool(ret_val, released);
    }
    return OPRT_OK;
}

/* [PATCH-LAMP-MCP] 收起机械臂：先回基准收纳位、**位置反馈确认到位后**再释放力矩。
 * 由 /usr/local/sbin/arm_fold.py 完成（不能在 MCP 回调里分步 system()：
 * smooth_goto 按时间插值、大跨度时舵机跟不上，按时间卸力会半路掉臂）。
 * arm_fold.py 流程：torque on 锁位 -> goto base_pose -> 轮询 read_all
 * 直到所有关节 |cur-target|<=30 tick 或超时 8s -> torque off -> 输出一行 JSON。 */
static OPERATE_RET __arm_release(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    BOOL_T settled = FALSE;
    BOOL_T released = FALSE;
    BOOL_T moved = FALSE;
    cJSON *pos_obj = NULL;

    /* 1. 停掉人脸跟随（若没在跑，stop 无副作用；stop 不会触发 Restart=always） */
    system("systemctl is-active --quiet face-follow && systemctl stop face-follow >/dev/null 2>&1");

    /* 2. 执行安全收臂脚本（阻塞直到到位或超时，约 1~8s） */
    FILE *fp = popen("/root/armenv/bin/python /usr/local/sbin/arm_fold.py --ms 1500 2>/dev/null", "r");
    if (fp) {
        char line[512];
        while (fgets(line, sizeof(line), fp)) {
            char *j = strchr(line, '{');
            if (!j) {
                continue;
            }
            cJSON *res = cJSON_Parse(j);
            if (!res) {
                continue;
            }
            cJSON *v;
            v = cJSON_GetObjectItem(res, "released");
            if (v && cJSON_IsBool(v)) released = cJSON_IsTrue(v);
            v = cJSON_GetObjectItem(res, "settled");
            if (v && cJSON_IsBool(v)) settled = cJSON_IsTrue(v);
            v = cJSON_GetObjectItem(res, "moved");
            if (v && cJSON_IsBool(v)) moved = cJSON_IsTrue(v);
            pos_obj = cJSON_DetachItemFromObjectCaseSensitive(res, "positions");
            cJSON_Delete(res);
        }
        pclose(fp);
    }
    PR_DEBUG("arm release: released=%d settled=%d moved=%d", released, settled, moved);

    /* 3. 组装返回给云端大模型 */
    cJSON *json = cJSON_CreateObject();
    if (json) {
        cJSON_AddBoolToObject(json, "released", released);
        cJSON_AddBoolToObject(json, "moved_to_home", moved);
        cJSON_AddBoolToObject(json, "settled", settled);
        cJSON_AddStringToObject(json, "action", settled
            ? "arm moved to home pose, position confirmed, then torque released"
            : "arm released (arrival not fully confirmed within timeout)");
        if (pos_obj) {
            cJSON_AddItemToObject(json, "positions", pos_obj);
        }
        ai_mcp_return_value_set_json(ret_val, json);
    } else {
        ai_mcp_return_value_set_bool(ret_val, released);
    }

    return OPRT_OK;
}

static OPERATE_RET __set_mode(const MCP_PROPERTY_LIST_T *properties, MCP_RETURN_VALUE_T *ret_val, void *user_data)
{
    AI_CHAT_MODE_E mode = 0;

    ai_mode_get_curr_mode(&mode);

    // Parse properties to get volume
    for (int i = 0; i < properties->count; i++) {
        MCP_PROPERTY_T *prop = properties->properties[i];
        if (strcmp(prop->name, "mode") == 0 && prop->type == MCP_PROPERTY_TYPE_INTEGER) {
            mode = prop->default_val.int_val;
            break;
        }
    }

    // Implement actual volume setting logic here
    OPERATE_RET rt = ai_mode_switch(mode);

    PR_DEBUG("set mode to %d rt:%d", mode, rt);

    // Set return value
    ai_mcp_return_value_set_bool(ret_val, (rt == OPRT_OK) ? TRUE : FALSE);

    return OPRT_OK;
}

static OPERATE_RET __ai_mcp_tools_register(void)
{
    OPERATE_RET rt = OPRT_OK;

    // device info get tool
    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_info_get",
        "Get device information such as model, serial number, and firmware version.",
        __get_device_info,
        NULL
    ), err);

#if defined(ENABLE_COMP_AI_VIDEO) && (ENABLE_COMP_AI_VIDEO == 1)
    // device camera take photo tool
    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_camera_take_photo",
        "Captures one or more photos using the device camera. \n"
        "Do NOT call this function when the user mentions printing, print, or printer.\n"
        "Only use when picture or scene change is detected, for visitor detection, or when the user asks for a photo.\n"
        "Parameters:\n"
        "- count (int): Number of photos to capture (1-10).\n"
        "Returns: Captured image(s) in Base64 format.",
        __take_photo,
        NULL,
        MCP_PROP_STR("question", "The question prompting the photo capture."),
        MCP_PROP_INT_DEF_RANGE("count", "Number of photos to capture (1-10).", 1, 1, 10)
    ), err);
#endif

#if defined(ENABLE_COMP_AI_AUDIO) && (ENABLE_COMP_AI_AUDIO == 1)
    // set volume tool
    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_audio_volume_set",
        "Sets the device's volume level.\n"
        "Parameters:\n"
        "- volume (int): The volume level to set (0-100).\n"
        "Response:\n"
        "- Returns true if the volume was set successfully.",
        __set_volume,
        NULL,
        MCP_PROP_INT_RANGE("volume", "The volume level to set (0-100).", 0, 100)
    ), err);
#endif

    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_audio_mode_set",
        "Sets the device's chat mode.\n"
        "Parameters:\n"
        "- mode (integer): The chat mode (0=hold, 1=key_press, 2=wakeup, 3=free).\n"
        "Response:\n"
        "- Returns true if the mode was set successfully.",
        __set_mode,
        NULL,
        MCP_PROP_INT_RANGE("mode", "The chat mode (0=hold, 1=key_press, 2=wakeup, 3=free)", 0, 3)
    ), err);

    /* [PATCH-LAMP-MCP] LED 灯环控制工具 */
    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_led_control",
        "Control the WS2812 LED ring on the robot lamp head.\n"
        "Pass only the parameters the user asked for (all optional):\n"
        "- power (string): 'on' to turn on, 'off' to turn off.\n"
        "- r, g, b (int 0-255): color channels, e.g. red = r:255,g:0,b:0. If only some channels are given, others keep current value.\n"
        "- brightness (int 0-100): brightness level.\n"
        "- effect (string): 'static' solid color (default), 'breath' breathing light, 'running' running light, 'rainbow' rainbow cycling, 'none' all off.\n"
        "- speed (int 1-10): effect speed, 1 slowest 10 fastest (default 5).\n"
        "Examples: 'turn on the light' -> power:on; 'change to a breathing red light' -> power:on, r:255,g:0,b:0, effect:breath.\n"
        "Returns: current LED ring state as JSON.",
        __led_control,
        NULL,
        MCP_PROP_STR("power", "'on' or 'off'"),
        MCP_PROP_INT_RANGE("r", "red channel 0-255", 0, 255),
        MCP_PROP_INT_RANGE("g", "green channel 0-255", 0, 255),
        MCP_PROP_INT_RANGE("b", "blue channel 0-255", 0, 255),
        MCP_PROP_INT_RANGE("brightness", "brightness 0-100", 0, 100),
        MCP_PROP_STR("effect", "'static','breath','running','rainbow' or 'none'"),
        MCP_PROP_INT_RANGE("speed", "effect speed 1-10", 1, 10)
    ), err);

    /* [PATCH-LAMP-MCP] 人脸跟随开关工具 */
    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_face_follow",
        "Enable or disable the face-following feature: the lamp head uses the camera to track the nearest human face and follow it, starting from the arm's current pose (no fixed start position; the arm keeps whatever shape it was posed in).\n"
        "Parameters:\n"
        "- enable (string): 'on' to start tracking, 'off' to stop.\n"
        "Use it when the user asks the lamp to look at / follow / face them, or to stop following.\n"
        "Returns: true if success.",
        __face_follow,
        NULL,
        MCP_PROP_STR("enable", "'on' or 'off'")
    ), err);

    /* [PATCH-LAMP-MCP] 迎宾模式工具 */
    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_greet_mode",
        "Enter or exit greet (welcome) mode: the arm keeps its current pose (no movement to any fixed position), and when a person walks in front of the camera, the lamp head swings side to side around its current direction and the robot greets them by voice, telling them to say 'nihao xiaoya' to start interacting.\n"
        "Parameters:\n"
        "- enable (string): 'on' to start greet mode, 'off' to exit.\n"
        "Use it when the user asks to start welcome/greet/reception mode, or to stop it.\n"
        "Returns: true if success.",
        __greet_mode,
        NULL,
        MCP_PROP_STR("enable", "'on' or 'off'")
    ), err);

    /* [PATCH-LAMP-MCP] 手动调节机械臂工具（软跟随+松手锁定，等效重力补偿） */
    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_arm_manual",
        "Enable or disable manual arm adjustment mode: the user can hold and reshape the lamp robot arm with their hand. While held, the joints feel light and follow the hand; when released, the arm holds its new shape firmly against gravity.\n"
        "Parameters:\n"
        "- enable (string): 'on' to enter manual adjustment mode, 'off' to exit (keeps the current pose locked).\n"
        "Use it when the user asks to adjust the arm by hand, pose the arm manually, or to exit manual mode.\n"
        "Returns: true if success.",
        __arm_manual,
        NULL,
        MCP_PROP_STR("enable", "'on' or 'off'")
    ), err);

    /* [PATCH-LAMP-MCP] 纯卸力工具（不移动关节，直接松开所有舵机） */
    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_arm_relax",
        "Release all servo torque so the lamp robot arm goes completely limp (free to be posed by hand). No joint moves by itself; the arm may sag under gravity.\n"
        "No parameters.\n"
        "Use it when the user asks to release the arm, relax the arm, cut power to the servos, or make the arm go limp ('xie li', 'song kai').\n"
        "For folding the arm back to its home pose first and then releasing, use device_arm_release instead.\n"
        "Returns: JSON with 'released' (true if all servos released).",
        __arm_relax,
        NULL
    ), err);

    /* [PATCH-LAMP-MCP] 收起机械臂工具 */
    TUYA_CALL_ERR_GOTO(AI_MCP_TOOL_ADD(
        "device_arm_release",
        "Fold the robot arm safely: stops face following (if running), re-engages torque, moves the arm to the saved home pose (stored in /etc/arm-pose.json), polls the real servo positions until every joint has arrived (30-tick tolerance), then releases all servo torque so the arm rests there and can be posed by hand.\n"
        "Use it when the user asks to fold the arm, put the arm away, release the arm, or power down the servos.\n"
        "No parameters.\n"
        "Returns: JSON with 'released', 'moved_to_home' and current servo 'positions'.",
        __arm_release,
        NULL
    ), err);

    return OPRT_OK;

err:
    // destroy MCP server on failure
    ai_mcp_server_destroy();

    return rt;
}

static OPERATE_RET __ai_mcp_init(void *data)
{
    OPERATE_RET rt = OPRT_OK;

    // FIXME: Set actual MCP server name and mcp version
    TUYA_CALL_ERR_RETURN(ai_mcp_server_init("Tuya MCP Server", "1.0"));

    TUYA_CALL_ERR_RETURN(__ai_mcp_tools_register());

    PR_DEBUG("MCP Server initialized successfully");

    return rt;
}

OPERATE_RET ai_mcp_init(void)
{
    return tal_event_subscribe(EVENT_MQTT_CONNECTED, "ai_mcp_init", __ai_mcp_init, SUBSCRIBE_TYPE_ONETIME);
}

OPERATE_RET ai_mcp_deinit(void)
{
    ai_mcp_server_destroy();

    PR_DEBUG("MCP Server deinitialized successfully");

    return OPRT_OK;
}
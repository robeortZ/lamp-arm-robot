# Lamp Arm Robot — 会迎宾的台灯机械臂

基于 **TuyaOpen + DshanPi-A1（RK3576，LINUX 平台）** 的台灯机器人：
5 舵机机械臂（Feetech STS3215）托着一盏灯，支持语音对话、人脸跟随、迎宾打招呼、
**手动调节机械臂形状（松手稳定）**、WS2812 灯环灯光控制，全部能力通过 MCP 工具
暴露给云端大模型，纯语音操控。

> 本仓库是完整复刻包：脚本、systemd 服务、配置、TuyaOpen 覆盖文件、app 源码全在内。

## 功能总览

| 功能 | 说明 | 触发方式 |
|---|---|---|
| 语音对话 | 云端 Agent（涂鸦云），本地 sherpa 唤醒词「你好小鸭」 | 语音 |
| 手动调节模式 | 手掰机械臂调形状，松手稳定住（等效重力补偿） | 语音/网页调参 |
| 迎宾模式 | 人靠近→灯头摆 3 下→云端 TTS 打招呼；人离开关灯 | 语音 |
| 人脸跟随 | 灯头追脸（YuNet），小臂协动 | 语音 |
| 灯环控制 | 开关/颜色/呼吸/流水/彩虹/亮度 | 语音（MCP） |
| 收起机械臂 | 归收纳位（位置反馈确认到位）→ 卸力 | 语音 |
| 卸力 | 全舵机释放力矩，机械臂变软 | 语音 |
| 网页调参 | 每关节触发灵敏度/触发力实时调节 | http://板子IP:8090 |

模式互斥：迎宾 / 人脸跟随 / 手动调节 systemd 双向互斥（Conflicts），
语音切换自动衔接（关闭跟随/迎宾 → 自动回手动模式）。

## 目录结构

```
scripts/    板载 Python/Shell 脚本（部署到 /usr/local/sbin、/root）
  arm_ctl.py          机械臂底层控制（扫描/读位/校准/限位/动作）
  arm_manual.py       手动调节模式主程序（软跟随+松手锁定+网页调参:8090）
  arm_relax.py        纯卸力（全舵机 torque off）
  arm_fold.py         安全收臂（位置反馈轮询确认到位才卸力）
  arm_savefold.py     保存当前位姿为收纳位
  arm_actions.py      动作库（点头/摇头/挥手等）
  arm_do.py           一次性小动作统一入口
  greet_service.py    迎宾模式（YuNet 人脸 + MOG2 运动前景检测）
  face_follow.py      人脸跟随（摄像头 + 舵机随动）
  pose_helper.py      位姿辅助
  audio-mixer-init.sh 声卡 mixer 初始化（ALC Off + Digital 192）
systemd/    服务单元（部署到 /etc/systemd/system）
  chatbot.service       TuyaOpen 主程序（注意 WorkingDirectory 必须=KV 所在目录）
  arm-manual.service    手动模式（Conflicts= greet/face-follow）
  greet.service         迎宾模式
  face-follow.service   人脸跟随
  sherpa-wakeup.service 本地唤醒词（sherpa-onnx，模型需自备）
  audio-mixer.service / no-suspend.service
config/     板上 /etc 配置（舵机校准/限位/收纳位/力矩参数）
app-your_chat_bot/   TuyaOpen 应用（拷到 apps/tuya.ai/your_chat_bot）
tuyaopen-overlay/    对开源 TuyaOpen 的改动文件（按相对路径覆盖）
```

## 硬件

- 主控：DshanPi-A1（RK3576，Armbian/Ubuntu，TuyaOpen LINUX 平台）
- 机械臂：LeRobot SO-101 风格 5×Feetech STS3215（1 底座pan / 2 大臂 / 3 小臂 /
  4 灯头俯仰 / 5 灯头自转），USB 串口 `/dev/serial/by-id/usb-1a86_*-if00`
- 灯环：WS2812 ×24（SPI2 内核模块 `/dev/ws2812-leds`，GRB 3 字节/灯）
- 摄像头：任意 UVC USB 相机（video 编号会漂移，脚本自动探测）
- 音频：板载 ES8323（采集端 32k→16k 2:1 降采样补丁见 overlay）

## 复刻步骤

### 1. 准备 TuyaOpen

```bash
git clone https://github.com/tuya/TuyaOpen && cd TuyaOpen
export TUYAOPEN_CN_DOWNLOAD=1   # 国内下载加速
```

### 2. 覆盖改动文件

把 `tuyaopen-overlay/` 下的文件按相对路径覆盖到 TuyaOpen 仓库：

```
src/peripherals/audio_codecs/tdd_audio/src/tdd_audio_alsa.c   # 32k→16k 降采样
platform/LINUX/tuyaos_adapter/tkl_uart.c, tkl_wired.c
platform/LINUX/tuyaos_adapter/tkl_kws.c, tkl_vad.c            # KWS/VAD 补丁
platform/LINUX/compiler/compiler_setup.cmake
boards/LINUX/DshanPi_A1/board_com_api.c                        # A1 板级
src/ai_components/ai_audio/src/ai_audio_input.c                # 预滚加大/线程同步
src/ai_components/ai_mode/src/ai_mode_free.c                   # 空闲 120s 回待命
src/ai_components/ai_mcp/src/ai_mcp_tools.c                    # 10 个 MCP 工具
```

### 3. 部署 app 并编译

```bash
cp -r app-your_chat_bot apps/tuya.ai/your_chat_bot
# 必改：app-your_chat_bot/include/tuya_config.h 里的 PID/UUID/AUTHKEY
# 必改：app_default.config 的 CONFIG_TUYA_PRODUCT_ID
cd apps/tuya.ai/your_chat_bot
python3 ../../tos.py build     # ⚠️ 固件在跑时编译会 Text file busy，先停服务
sudo systemctl stop chatbot ; python3 ../../tos.py build ; sudo systemctl start chatbot
```

### 4. 部署脚本与服务

```bash
sudo cp scripts/*.py /usr/local/sbin/ && sudo chmod +x /usr/local/sbin/*.py
sudo cp scripts/*.sh /usr/local/sbin/ 2>/dev/null
sudo cp scripts/arm_do.py scripts/arm_actions.py /root/
sudo cp config/*.json /etc/
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chatbot arm-manual
```

Python 依赖（板子）：`feetech-servo-sdk`（armenv 虚拟环境）、`opencv-python`、
`numpy`。sherpa-onnx 唤醒词模型（tokens/encoder/decoder/joiner + mnn）体积大，
**不在本仓库**，参考 sherpa-onnx 官方文档自备，路径见 sherpa-wakeup.service。

### 5. 机械臂校准（换臂必做）

```bash
sudo systemctl stop arm-manual
python3 /usr/local/sbin/arm_ctl.py scan            # 确认 1~5 号在线
python3 /usr/local/sbin/arm_ctl.py zero            # 中位校准
python3 /usr/local/sbin/arm_ctl.py limits          # 行程限位
# 手掰到想要的姿态后保存基准位 / 收纳位：
python3 /usr/local/sbin/arm_ctl.py savehome
sudo /root/armenv/bin/python /usr/local/sbin/arm_savefold.py   # 存收纳位
```

### 6. 网页调参

浏览器打开 `http://<板子IP>:8090`：每关节「触发灵敏度」（偏离多少 tick 变软）
和「触发力」（负载多少 % 变软），滑块即时生效，存 `/etc/arm-manual-torque.json`。

## ⚠️ 关键实验结论：STS3215 EEPROM 参数运行时不生效

行为级实验证实（同行程计时对比）：**Max Torque(reg16)、位置环 PID(reg26-29)
都是上电锁存的 EEPROM 值，运行时写入/使能重采样均不生效**。
所以本项目的「重力补偿手感」完全靠**跟随循环**实现：

- HOLD：位置环满刚度锁定 → 松手稳定
- 触发（偏离超阈值 **或** 负载超阈值「一碰就软」）→ FOLLOW：每周期目标位=实际位置，
  误差趋零 → 手感轻
- 静止 0.35s → 冻结目标 → 刚度恢复

这决定了参数只有「触发灵敏度/触发力」两类，而不是力矩值。

## 其他注意事项

- **chatbot.service 的 WorkingDirectory 必须=固件 KV 所在目录**，换目录跑会变成新设备
- 声卡独占：重启 chatbot 时 stop 后**等 8 秒再 start**，否则旧实例声卡未释放 →
  EBUSY → MCP 工具静默全丢
- 验证启动三要素（/var/log.hdd/tb-app.log）：`MCP Server initialized successfully`
  + 工具 `Added tool:` 行
- 重启后舵机总线头 1~2 分钟可能无响应（上电时序），等再试
- UVC 编号漂移已由脚本自动探测兜底

## 许可

仅供学习研究。TuyaOpen 部分遵循其原始开源协议；涂鸦云接入需自行申请
PID / UUID / AUTHKEY（平台：platform.tuya.com）。

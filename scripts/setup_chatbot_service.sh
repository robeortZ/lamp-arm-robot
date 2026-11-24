#!/bin/sh
# 把 TuyaOpen your_chat_bot 装成开机自启服务（DshanPi-A1）
# 注意：KV(tuyadb) 相对工作目录，必须固定 WorkingDirectory，否则会变成"新设备"重新激活
set -e

BIN=/root/TuyaOpen/apps/tuya.ai/your_chat_bot/.build/bin/your_chat_bot_1.0.1.elf
WD=/root/tb_run
mkdir -p "$WD"

cat > /etc/systemd/system/chatbot.service <<EOF
[Unit]
Description=TuyaOpen your_chat_bot (DshanPi A1 lamp)
After=network-online.target sound.target audio-mixer.service
Wants=network-online.target
# 声卡独占：同一时刻只能跑一个实例

[Service]
Type=simple
WorkingDirectory=$WD
ExecStart=$BIN
Restart=on-failure
RestartSec=5
# 日志写真实磁盘，重启不丢
StandardOutput=append:/var/log.hdd/tb-app.log
StandardError=append:/var/log.hdd/tb-app.log

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/stop_chatbot.sh <<'EOS'
#!/bin/sh
systemctl stop chatbot.service 2>/dev/null || true
# 兜底：按可执行文件名杀残留（别用 pkill -f 全路径，会连带杀掉自己的 shell）
for p in $(ls /proc | grep -E '^[0-9]+$'); do
    [ -r /proc/$p/comm ] || continue
    case "$(cat /proc/$p/comm 2>/dev/null)" in
        your_chat_bot_1*) kill -TERM "$p" 2>/dev/null ;;
    esac
done
for i in 1 2 3 4 5; do
    pgrep -c your_chat_bot_1 >/dev/null 2>&1 || return 0 2>/dev/null || exit 0
    sleep 1
done
for p in $(ls /proc | grep -E '^[0-9]+$'); do
    [ -r /proc/$p/comm ] || continue
    case "$(cat /proc/$p/comm 2>/dev/null)" in
        your_chat_bot_1*) kill -KILL "$p" 2>/dev/null ;;
    esac
done
EOS
chmod +x /usr/local/sbin/stop_chatbot.sh

systemctl daemon-reload
systemctl enable chatbot.service
echo "已安装 chatbot.service，WorkingDirectory=$WD"
systemctl is-enabled chatbot.service

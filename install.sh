#!/bin/bash
# install.sh — 在 DshanPi-A1（或其他 Armbian/RK3576 板子）上部署台灯机械臂
# 用法：把本仓库拷到板子后，sudo bash install.sh
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "[1/4] 部署脚本..."
sudo cp "$REPO"/scripts/*.py "$REPO"/scripts/*.sh /usr/local/sbin/ 2>/dev/null || true
sudo cp "$REPO"/scripts/arm_do.py "$REPO"/scripts/arm_actions.py /root/
sudo chmod +x /usr/local/sbin/*.py /usr/local/sbin/*.sh 2>/dev/null || true

echo "[2/4] 部署配置..."
sudo cp "$REPO"/config/*.json /etc/

echo "[3/4] 部署 systemd 服务..."
sudo cp "$REPO"/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload

echo "[4/4] 启用服务..."
# chatbot 固件路径若与 service 中不同，先改 /etc/systemd/system/chatbot.service 的 ExecStart
sudo systemctl enable --now chatbot.service arm-manual.service 2>/dev/null || \
    echo "!! chatbot 未启动（先按 README 编译固件并改好 ExecStart 路径）"

echo "完成。网页调参: http://<板子IP>:8090"
echo "换新机械臂务必先校准: systemctl stop arm-manual && arm_ctl.py scan/zero/limits/savehome"

#!/bin/bash
# wifi_selftest.sh — 配网门户全链路自测（在板上跑，SSH 断开不影响）
exec > /tmp/wifi_selftest.log 2>&1
echo "=== selftest start $(date +%T)"
# 清掉可能残留的调试门户
pkill -f 'wifi_setup[.]py portal --no-ap' 2>/dev/null
sleep 1

PSK=$(grep -h 'psk=' '/run/NetworkManager/system-connections/netplan-NM-a13a4c20-6214-45dd-8431-418103538009-TYTY2.nmconnection' | sed 's/psk=//' | tr -d ' \t')
echo "PSK 读取: ${PSK:0:4}***"

echo "--- 启动配网门户(真实AP)..."
systemctl start wifi-setup
sleep 32
echo "--- portal 状态:"
curl -s --max-time 5 http://192.168.0.1/api/status; echo
echo "--- 模拟手机提交 TYTY2 凭证:"
curl -s --max-time 5 -X POST http://192.168.0.1/api/connect \
  -H 'Content-Type: application/json' \
  -d "{\"ssid\":\"TYTY2\",\"password\":\"$PSK\"}"; echo

PH=""
for i in $(seq 1 50); do
  sleep 3
  PH=$(/root/armenv/bin/python -c "import json;print(json.load(open('/run/wifi-setup/status.json')).get('phase',''))" 2>/dev/null)
  echo "[$i] phase=$PH $(date +%T)"
  if [ "$PH" = "connected" ]; then break; fi
done
echo "=== end $(date +%T) 最终 phase=$PH"
systemctl is-active wifi-setup

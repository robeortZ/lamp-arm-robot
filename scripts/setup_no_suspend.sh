#!/bin/sh
# 在板子上禁用一切系统休眠，并把 mem_sleep 固定为 s2idle。
# 用法: sh setup_no_suspend.sh
set -e

echo "=== 1. systemd sleep.conf 禁用全部睡眠动作 ==="
mkdir -p /etc/systemd
if [ -f /etc/systemd/sleep.conf ] && ! grep -q 'noSuspend-2026' /etc/systemd/sleep.conf; then
    cp -a /etc/systemd/sleep.conf /etc/systemd/sleep.conf.bak-noSuspend
fi
cat > /etc/systemd/sleep.conf <<'EOF'
# noSuspend-2026: 由复刻脚本写入，禁止一切系统睡眠
[Sleep]
AllowSuspend=no
AllowHibernation=no
AllowSuspendThenHibernate=no
AllowHybridSleep=no
EOF
cat /etc/systemd/sleep.conf

echo "=== 2. 开机把 mem_sleep 固定为 s2idle ==="
cat > /etc/systemd/system/no-suspend.service <<'EOF'
[Unit]
Description=Force s2idle and forbid deep suspend (DshanPi A1)
DefaultDependencies=no
After=local-fs.target
Before=sysinit.target shutdown.target
Conflicts=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'if [ -w /sys/power/mem_sleep ]; then echo s2idle > /sys/power/mem_sleep; fi'
ExecStart=/bin/sh -c 'echo "no-suspend: mem_sleep=$(cat /sys/power/mem_sleep 2>/dev/null)" | systemd-cat -t no-suspend'

[Install]
WantedBy=sysinit.target
EOF
systemctl daemon-reload
systemctl enable no-suspend.service
systemctl start no-suspend.service
systemctl is-enabled no-suspend.service

echo "=== 3. 复查 ==="
echo "mem_sleep: $(cat /sys/power/mem_sleep)"
for t in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
    printf '%-22s %s\n' "$t" "$(systemctl is-enabled $t 2>&1)"
done
echo "--- 试一下 systemctl suspend 是否被拒 ---"
systemctl suspend 2>&1 || true

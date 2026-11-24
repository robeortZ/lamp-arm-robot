#!/bin/sh
# 黑匣子：每 5 秒把系统关键状态追加写到真实磁盘，用于事后定位板子"跑着跑着就停"的原因。
# 日志: /var/log.hdd/blackbox.log （重启不清空，超过 2MB 自动轮转一份 .1）
set -e

cat > /usr/local/sbin/blackbox.sh <<'EOS'
#!/bin/sh
LOG=/var/log.hdd/blackbox.log
while true; do
    if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG" 2>/dev/null || echo 0)" -gt 2097152 ]; then
        mv -f "$LOG" "$LOG.1"
    fi
    {
        printf '%s | up %s | ' "$(date '+%F %T')" "$(cut -d. -f1 /proc/uptime)"
        printf 'load %s | ' "$(cut -d' ' -f1-3 /proc/loadavg)"
        printf 'temp %s | ' "$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)"
        printf 'mem %s | ' "$(awk '/MemAvailable/{print int($2/1024)"M free"}' /proc/meminfo)"
        printf 'sleep=%s ' "$(cat /sys/power/mem_sleep 2>/dev/null)"
        echo
        dmesg | tail -n 3 | sed 's/^/    kern: /'
    } >> "$LOG"
    sleep 5
done
EOS
chmod +x /usr/local/sbin/blackbox.sh

cat > /etc/systemd/system/blackbox.service <<'EOS'
[Unit]
Description=Blackbox recorder (state snapshot every 5s)
After=local-fs.target var-log.hdd.mount

[Service]
Type=simple
ExecStart=/usr/local/sbin/blackbox.sh
Restart=always
RestartSec=3
Nice=10

[Install]
WantedBy=multi-user.target
EOS

systemctl daemon-reload
systemctl enable --now blackbox.service
sleep 6
systemctl is-active blackbox.service
echo "--- 首条记录 ---"
tail -n 6 /var/log.hdd/blackbox.log

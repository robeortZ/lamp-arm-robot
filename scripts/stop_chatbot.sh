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

#!/bin/sh
# 等声卡就绪
for i in $(seq 1 40); do
    [ -e /proc/asound/card0 ] && break
    sleep 0.25
done

C=0
# 播放：数字音量拉到 0dB，输出级 0dB，各级开关全开
amixer -c $C cset numid=56 192 >/dev/null 2>&1   # PCM Volume  0dB
amixer -c $C cset numid=59 30  >/dev/null 2>&1   # Output 1   0dB
amixer -c $C cset numid=60 30  >/dev/null 2>&1   # Output 2   0dB
amixer -c $C cset numid=80 on  >/dev/null 2>&1   # OUT1 Switch
amixer -c $C cset numid=81 on  >/dev/null 2>&1   # OUT2 Switch
amixer -c $C cset numid=76 on  >/dev/null 2>&1   # Left Mixer Playback
amixer -c $C cset numid=78 on  >/dev/null 2>&1   # Right Mixer Playback
amixer -c $C sset 'Speaker' on >/dev/null 2>&1
amixer -c $C sset 'Headphone' on >/dev/null 2>&1

# 录音：ADC 0dB，不开 ALC（否则 TTS 回采/距离变化会让增益乱跳）
amixer -c $C cset numid=50 192 >/dev/null 2>&1   # Capture Digital 0dB
amixer -c $C cset numid=51 off >/dev/null 2>&1   # Capture Mute off
amixer -c $C cset numid=68 on  >/dev/null 2>&1   # Main Mic Switch
amixer -c $C cset numid=41 0  >/dev/null 2>&1    # ALC Capture Function = Off

alsactl store >/dev/null 2>&1 || true
echo "audio-mixer-init: PCM=$(amixer -c $C cget numid=56 | grep -o 'values=.*')"

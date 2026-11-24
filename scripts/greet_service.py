#!/root/armenv/bin/python
# -*- coding: utf-8 -*-
"""greet_service.py — 迎宾模式服务 v4

[PATCH-MANUAL] v4：不再展开到固定迎宾位、不再自动收臂——保留用户在手动模式下
摆好的形状。启动只锁当前姿态；检测到人 → 灯头(5号)围绕当前位置 ±30° 摆 3 下
+ 写 /run/greet_hi(TTS+彩虹灯效)；人离开 → 写 /run/greet_bye(chatbot 关灯)。

检测：YuNet 人脸 OR MOG2 运动前景（手/脚/身体任何部位在画面里就算有人）
摆头：中心=启动时 5 号当前位置（安全收缩到行程内）
退出：SIGTERM 优雅退出，机械臂保持当前形状锁定（卸力请用「卸力」功能）

用法：greet_service.py [--amp 340] [--cycles 3] [--cooldown 45]
                       [--away-sec 8] [--motion-area 0.02]
"""
import sys
import os
import json
import time
import signal
import argparse
import glob

import cv2

sys.path.insert(0, "/usr/local/sbin")
import arm_ctl as A

HI_FILE = "/run/greet_hi"
BYE_FILE = "/run/greet_bye"
MODEL = "/root/models/face_detection_yunet_2023mar.onnx"

RUNNING = True

def _on_term(signum, frame):
    global RUNNING
    RUNNING = False

signal.signal(signal.SIGTERM, _on_term)
signal.signal(signal.SIGINT, _on_term)


def find_uvc_cam():
    """[PATCH-UVC-DRIFT] UVC video 编号重启后会漂移，按 USB 链路自动探测"""
    cands = []
    for d in sorted(glob.glob("/sys/class/video4linux/video*"),
                    key=lambda p: int(p.rsplit("video", 1)[1])):
        try:
            if "/usb" not in os.path.realpath(os.path.join(d, "device")):
                continue
        except Exception:
            continue
        cands.append("/dev/" + os.path.basename(d))
    for dev in cands:
        try:
            cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
            ok = cap.isOpened() and cap.read()[0]
            cap.release()
            if ok:
                print("UVC 摄像头自动探测: %s" % dev)
                return dev
        except Exception:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--amp", type=int, default=340, help="摆头幅度 tick（±30°≈340）")
    ap.add_argument("--cycles", type=int, default=3, help="摆头来回次数")
    ap.add_argument("--cooldown", type=float, default=45.0, help="两次招呼最小间隔 s")
    ap.add_argument("--away-sec", type=float, default=8.0, help="人消失多久算离开 s")
    ap.add_argument("--score", type=float, default=0.5, help="YuNet 人脸阈值")
    ap.add_argument("--motion-area", type=float, default=0.02,
                    help="MOG2 前景面积占比阈值（画面比例，手/脚/身体即触发）")
    args = ap.parse_args()

    bus = A.Bus("auto", None)
    alive = [i for i in (1, 2, 3, 4, 5) if bus.ping(i) is not None]
    if not alive:
        print("!! 没有响应的舵机，退出")
        return 1
    limits = A.load_limits()
    lo5, hi5 = limits.get(5, [1181, 3104])

    def lamp_write(cmd):
        try:
            bus.write_goal(5, max(lo5, min(hi5, int(cmd))))
        except Exception as e:
            print("!! 5 号写入失败: %s" % e)

    # 启动：锁当前姿态（不移动到任何固定位置，保留手调形状）
    grip = A.arm_grip(bus, alive)
    if not grip:
        print("!! 使能力矩失败，退出")
        return 1
    print("迎宾: 已锁定当前姿态 %s（不再展开到固定迎宾位）"
          % {s: grip[s] for s in sorted(grip)})

    # 摆头中心 = 启动时 5 号当前位置（安全收缩到行程内）
    cur5 = grip.get(5, (lo5 + hi5) // 2)
    lamp_cmd = [min(hi5, max(lo5, cur5))]
    tgt5_raw = lamp_cmd[0]

    def lamp_sweep():
        """灯头以当前位置为中心来回摆 cycles 个来回（两端停顿，清晰可数），
        最后回到中心。中心自动收缩到行程内，避免贴行程上限单边被钳掉。"""
        center = max(lo5 + args.amp + 20, min(hi5 - args.amp - 20, min(hi5, max(lo5, tgt5_raw))))
        seq = []
        for _ in range(args.cycles):
            seq.append(center + args.amp)
            seq.append(center - args.amp)
        seq.append(center)
        print("迎宾: 灯头摆头 %d 个来回 (center=%d range=[%d,%d])"
              % (args.cycles, center, center - args.amp, center + args.amp))
        for t in seq:
            while RUNNING and abs(lamp_cmd[0] - t) > 30:
                step = max(-40, min(40, t - lamp_cmd[0]))
                lamp_cmd[0] += step
                lamp_write(lamp_cmd[0])
                time.sleep(0.03)
            time.sleep(0.12)    # 两端各停一下，来回次数看得清
        lamp_cmd[0] = center
        lamp_write(center)
        print("迎宾: 摆头完成，灯头回中位 %d" % center)
        return center

    # 摄像头 + 检测器
    cam_dev = find_uvc_cam()
    if cam_dev is None:
        print("!! 未探测到 USB 摄像头（检查相机 USB 线）")
        return 1
    cam = cv2.VideoCapture(cam_dev, cv2.CAP_V4L2)
    if not cam.isOpened():
        print("!! 摄像头打开失败: %s" % cam_dev)
        return 1
    ok, frame = cam.read()
    if not ok:
        print("!! 摄像头读帧失败")
        return 1
    H, W = frame.shape[:2]
    det = cv2.FaceDetectorYN.create(MODEL, "", (W, H), args.score, 0.3, 5000)
    fgbg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=40, detectShadows=False)
    motion_area_min = W * H * args.motion_area
    print("迎宾服务启动: %dx%d, 动作面积阈值=%d px" % (W, H, motion_area_min))

    present = False
    absent_since = None
    last_hi = 0.0
    hit_buf = []             # 最近 5 帧「有人」命中窗口

    try:
        while RUNNING:
            ok, frame = cam.read()
            if not ok:
                time.sleep(0.02)
                continue
            now = time.time()

            # ---- 检测：人脸 OR 运动前景 ----
            _, faces = det.detect(frame)
            face_found = faces is not None and len(faces) > 0
            fgmask = fgbg.apply(frame)
            fgmask = cv2.erode(fgmask, None, iterations=1)
            motion = cv2.countNonZero(fgmask) > motion_area_min
            hit_buf.append(1 if (face_found or motion) else 0)
            if len(hit_buf) > 5:
                hit_buf.pop(0)
            person_now = sum(hit_buf) >= 3

            if person_now:
                if not present:
                    present = True
                    absent_since = None
                    # 新客人/再次出现：摆头 + 打招呼（冷却只管 TTS）
                    print("迎宾: 检测到有人 (face=%s motion=%s)" % (face_found, motion))
                    lamp_sweep()
                    if now - last_hi >= args.cooldown:
                        last_hi = now
                        try:
                            with open(HI_FILE, "w") as fp:
                                fp.write("rainbow:10")
                            print("迎宾: 已通知 TTS + 彩虹灯效")
                        except Exception as e:
                            print("!! 写通知文件失败: %s" % e)
            else:
                if present:
                    if absent_since is None:
                        absent_since = now
                    elif now - absent_since >= args.away_sec:
                        present = False
                        absent_since = None
                        print("迎宾: 人离开，通知关灯")
                        try:
                            with open(BYE_FILE, "w") as fp:
                                fp.write("bye")
                        except Exception:
                            pass

            time.sleep(0.03)
    finally:
        cam.release()
        # [PATCH-MANUAL] 退出不再收臂：保持当前形状锁定，只通知关灯
        try:
            with open(BYE_FILE, "w") as fp:
                fp.write("bye")
        except Exception:
            pass
        print("迎宾服务退出（保持当前形状锁定，力矩仍使能）")
    return 0

if __name__ == "__main__":
    sys.exit(main())

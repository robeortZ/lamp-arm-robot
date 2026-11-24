#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台灯人脸跟随 —— 检测(CV) + 姿态(PnP) + 死区/EMA/比例控制 + 机械臂

参考 tuyaopen.100ask.net「人脸跟随」的实现思路，但感知层换成
OpenCV YuNet + solvePnP（无需 RKNN / Hopenet），控制器沿用文档的
死区 + EMA + 比例增益。

与控制相关的关键差别（有意为之）：
  文档用 Hopenet 的 yaw 当误差，那是"头部相对相机的朝向"。
  人站在画面边上正对相机时 yaw≈0 → 文档那套不会转过去，跟不到人。
  这里用【人脸框中心相对画面中心的偏移】换算成角度作误差，才是"人在哪"。
  Hopenet 那类 yaw 更适合判断"有没有在看你"。

用法（先预览，不动机械臂）：
  /root/armenv/bin/python face_follow.py                # 只预览，不驱动
  /root/armenv/bin/python face_follow.py --armed --tilt          # 只动灯头俯仰
  /root/armenv/bin/python face_follow.py --armed --tilt --elbow  # 小臂也参与俯仰
  /root/armenv/bin/python face_follow.py --armed --pan --tilt    # 底座也动（底座修好后）
浏览器打开 http://<板子IP>:8081 看实时画面
"""

import argparse
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

sys.path.insert(0, "/usr/local/sbin")

TICK_PER_DEG = 4096.0 / 360.0


def _f(v, nd=2):
    """统一转成 Python float 再 round。
    NumPy 2 里 round(np.float32) 仍返回 np.float32，json 序列化会直接抛
    TypeError: Object of type float32 is not JSON serializable。"""
    try:
        return round(float(v), nd)
    except Exception:
        return 0.0


# ---------------------------------------------------------------- 摄像头

def find_uvc_cam():
    """[PATCH-UVC-DRIFT] 探测 USB(UVC) 摄像头设备节点。
    UVC 重启后 video 编号会漂移（11<->12），写死会开不出来。
    通过 /sys/class/video4linux/videoX/device 链路是否为 USB 设备，
    过滤掉 RK 内部 CIF 节点（stream_cif_*/rkcif_*），再逐个试开+读帧确认。"""
    import glob as _glob
    cands = []
    for d in sorted(_glob.glob("/sys/class/video4linux/video*"),
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
            ok_open = cap.isOpened()
            ok_read = ok_open and cap.read()[0]
            cap.release()
            if ok_read:
                print("UVC 摄像头自动探测: %s" % dev)
                return dev
        except Exception:
            continue
    return None


class Camera:
    def __init__(self, dev, w, h, fps):
        # [PATCH-UVC-DRIFT] dev=auto 或指定节点打不开时，自动探测 UVC 编号
        self.cap = None
        cands = []
        if dev and dev != "auto":
            cands.append(dev)
        auto = find_uvc_cam()
        if auto and auto not in cands:
            cands.append(auto)
        for c in cands:
            cap = cv2.VideoCapture(c, cv2.CAP_V4L2)
            if cap.isOpened():
                self.cap = cap
                self.dev = c
                print("摄像头打开: %s" % c)
                break
            cap.release()
        if self.cap is None:
            raise RuntimeError("打不开摄像头 %s（未探测到可用的 USB 摄像头，检查 USB 线）" % cands)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ok, _ = self.cap.read()
        if not ok:
            raise RuntimeError("摄像头读不到帧")
        self.w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def read(self):
        return self.cap.read()

    def close(self):
        self.cap.release()


# ---------------------------------------------------------------- 机械臂

class Arm:
    """把 pan(灯头水平/底座) / tilt(灯头俯仰) / elbow(小臂) 映射到具体舵机。
    默认情况下不使能、不写入，只有 armed=True 才会真正动。"""

    PAN_ID = 1     # 底座旋转
    TILT_ID = 4    # 灯头俯仰
    ELBOW_ID = 3   # 小臂（俯仰的粗调，慢速）

    def __init__(self, armed, use_pan, use_tilt, use_elbow=False,
                 port="auto", max_step=25, span_deg=30.0):
        self.armed = armed
        self.use_pan = use_pan
        self.use_tilt = use_tilt
        self.use_elbow = use_elbow
        self.max_step = max_step
        self.span_deg = span_deg
        self.bus = None
        self.A = None
        self.limits = {}
        self.envelope = {}
        self.cmd = {}
        self.base = {}      # 各轴使能时的起始位置（elbow 的绝对目标基准）
        self.errors = {}

    def open(self):
        import arm_ctl as A
        self.A = A
        self.limits = A.load_limits()
        try:
            self.limits = {int(k): v for k, v in json.load(open(A.LIMIT_FILE)).items()}
        except Exception:
            self.limits = dict(A.DEFAULT_LIMITS)

        self.bus = A.Bus("auto")
        # 全轴都管：参与跟随的轴做闭环，不参与的（大臂 ID2 / 灯头自转 ID5）
        # 也要上力矩锁住——否则大臂是软的，整灯重量全压在小臂上，姿态会被拖歪
        alive = [i for i in (1, 2, 3, 4, 5) if self.bus.ping(i) is not None]
        errs = A.check_servo_errors(self.bus, alive, release=False)
        self.errors = errs
        # 带错误的轴一律不动，避免二次堵转
        for i in list(errs):
            if i == self.PAN_ID:
                self.use_pan = False
            if i == self.TILT_ID:
                self.use_tilt = False
            if i == self.ELBOW_ID:
                self.use_elbow = False
        ids = [i for i in alive if self._enabled(i)]

        pose = self.bus.read_all(alive) or {}
        self.cmd = {i: pose.get(i, 2047) for i in alive}
        self.base = dict(self.cmd)
        # 机械限位尚未标定时，用「起点 ±span_deg」当安全围栏，
        # 避免跟随把关节顶到死角 → 过流保护 → 整条手臂甩出去
        span = int(self.span_deg * TICK_PER_DEG)
        for i in ids:
            c = self.cmd[i]
            self.envelope[i] = (max(0, c - span), min(4095, c + span))
        if self.armed:
            # 全轴上力矩：先各自锁在当前位置再使能，避免朝残留目标猛冲
            ok = A.arm_grip(self.bus, alive)
            if not ok:
                print("!! 使能力矩失败，机械臂保持不动")
                self.armed = False
            else:
                self.cmd.update(ok)
                self.base = dict(self.cmd)
                # [PATCH-MANUAL] 不再回基准位：保留用户手调的形状，
                # 跟随围栏以当前姿态为中心展开
                cur = self.bus.read_all(alive) or {}
                self.cmd.update({i: cur[i] for i in alive if i in cur})
                self.base = dict(self.cmd)
                # 安全围栏只套在参与跟随的轴上
                span = int(self.span_deg * TICK_PER_DEG)
                self.envelope = {i: (max(0, self.cmd[i] - span),
                                     min(4095, self.cmd[i] + span))
                                 for i in ids}
        return self

    def _enabled(self, sid):
        return ((sid == self.PAN_ID and self.use_pan)
                or (sid == self.TILT_ID and self.use_tilt)
                or (sid == self.ELBOW_ID and self.use_elbow))

    def clamp(self, sid, v):
        lo, hi = self.limits.get(sid, [0, 4095])
        elo, ehi = self.envelope.get(sid, (0, 4095))
        lo, hi = max(lo, elo), min(hi, ehi)
        return max(lo, min(hi, int(v)))

    def step(self, pan_abs_deg=None, tilt_abs_deg=None, elbow_abs_deg=None):
        """三个轴都按「相对基准位的绝对角度」推进（None = 这步不动）。
        内部做限速与限位（机械限位 + 起点围栏）。"""
        if not self.armed or self.bus is None:
            return
        out = {}
        for sid, abs_deg, on in ((self.PAN_ID, pan_abs_deg, self.use_pan),
                                 (self.TILT_ID, tilt_abs_deg, self.use_tilt),
                                 (self.ELBOW_ID, elbow_abs_deg, self.use_elbow)):
            if not on or abs_deg is None or sid not in self.base:
                continue
            tgt = self.clamp(sid, self.base[sid] + abs_deg * TICK_PER_DEG)
            d = max(-self.max_step, min(self.max_step, tgt - self.cmd[sid]))
            self.cmd[sid] = self.clamp(sid, self.cmd[sid] + d)
            out[sid] = self.cmd[sid]
        for sid, v in out.items():
            self.bus.write_goal(sid, v)

    def release(self):
        # [PATCH-MANUAL] 退出不再卸力（卸力会塌）：保持当前形状锁定，只关总线
        if self.bus is not None:
            self.bus.close()


# ---------------------------------------------------------------- 控制器

class FollowController:
    """EMA 平滑 + 死区 + 比例增益（误差单位：度，来自人脸框中心偏移）。

    俯仰采用「总修正量分配」：
      total = 灯头+小臂 累计需要的俯仰修正（度）
      slow  = 小臂已接管的份额，按时间常数 1/elbow_rate 向 total 收敛
      fast  = total - slow = 灯头俯仰承担的份额
    好处：人蹲下/站起这种大偏差，灯头先顶上去，小臂几秒内慢慢接手，
    灯头自动回中、双向余量一直在；人离开时 total 缓慢归零，整灯回待命。"""

    def __init__(self, kp=0.55, deadzone=5.0, ema=0.35,
                 elbow_rate=0.5, elbow_max=25.0, total_max=45.0,
                 return_rate=6.0, yaw_max=40.0):
        self.kp = kp
        self.deadzone = deadzone
        self.ema = ema
        self.elbow_rate = elbow_rate   # 小臂接管速度（1/s，0.5 ≈ 2 秒到 63%）
        self.elbow_max = elbow_max     # 小臂最多接管多少度
        self.total_max = total_max     # 俯仰累计修正上限
        self.return_rate = return_rate # 丢脸后回待命的速度（度/秒）
        self.yaw_max = yaw_max         # 底座累计修正上限
        self.sy = 0.0
        self.sp = 0.0
        self.seen = False
        self.total = 0.0
        self.slow = 0.0
        self.total_yaw = 0.0
        self.last_t = None

    def update(self, err_yaw, err_pitch, found):
        now = time.time()
        dt = 0.033
        if self.last_t is not None:
            dt = min(0.2, max(0.005, now - self.last_t))
        self.last_t = now

        if not found:
            self.seen = False
            self.sy *= 0.5
            self.sp *= 0.5
            # 回待命：总修正按 return_rate 缓慢归零（小臂跟着退）
            back = min(abs(self.total), self.return_rate * dt)
            self.total -= back if self.total > 0 else -back
            backy = min(abs(self.total_yaw), self.return_rate * dt)
            self.total_yaw -= backy if self.total_yaw > 0 else -backy
        else:
            if not self.seen:
                self.sy, self.sp = err_yaw, err_pitch
                self.seen = True
            else:
                self.sy = self.ema * err_yaw + (1 - self.ema) * self.sy
                self.sp = self.ema * err_pitch + (1 - self.ema) * self.sp
            dp = 0.0 if abs(self.sp) < self.deadzone else self.sp
            dy = 0.0 if abs(self.sy) < self.deadzone else self.sy
            # 关键：total 是「每秒转 kp*err 度」的速率积分，必须乘 dt。
            # 不乘 dt 的话每个控制周期(12Hz)都加 kp*err 度，
            # 等效角速度 124°/s，一帧就冲过目标 → 2.4 秒周期极限环振荡。
            self.total += self.kp * dp * dt
            self.total = max(-self.total_max, min(self.total_max, self.total))
            self.total_yaw += self.kp * dy * dt
            self.total_yaw = max(-self.yaw_max, min(self.yaw_max, self.total_yaw))

        self.slow += self.elbow_rate * (self.total - self.slow) * dt
        self.slow = max(-self.elbow_max, min(self.elbow_max, self.slow))
        fast = self.total - self.slow

        return self.total_yaw, fast, self.slow


# ---------------------------------------------------------------- MJPEG

class Preview:
    def __init__(self):
        self.lock = threading.Lock()
        self.jpeg = None
        self.extra = {}

    def set(self, jpeg, extra):
        with self.lock:
            self.jpeg = jpeg
            self.extra = extra

    def get(self):
        with self.lock:
            return self.jpeg, self.extra


def make_handler(preview):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    while True:
                        jpeg, _ = preview.get()
                        if jpeg:
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                            self.wfile.write(("Content-Length: %d\r\n\r\n" % len(jpeg)).encode())
                            self.wfile.write(jpeg)
                            self.wfile.write(b"\r\n")
                        time.sleep(0.04)
                except Exception:
                    return
            elif self.path.startswith("/status"):
                _, extra = preview.get()
                body = json.dumps(extra, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/snap.jpg"):
                jpeg, _ = preview.get()
                if not jpeg:
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
            else:
                html = (b"<!doctype html><meta charset=utf-8>"
                        b"<title>face follow</title>"
                        b"<body style='margin:0;background:#111;color:#ddd;font-family:sans-serif'>"
                        b"<img src='/stream' style='width:100%;max-width:960px;display:block'>"
                        b"<p style='padding:8px'>YuNet + solvePnP &nbsp;|&nbsp; "
                        b"<a style='color:#6cf' href='/status'>status</a></p></body>")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)

    return H


# ---------------------------------------------------------------- 姿态

# 通用 3D 人脸模型（mm），用于 solvePnP。与 YuNet 的 5 个关键点顺序对应
FACE_3D = np.array([
    [-34.0, 44.0, 0.0],    # 左眼外角
    [34.0, 44.0, 0.0],     # 右眼外角
    [0.0, 0.0, 0.0],       # 鼻尖
    [-26.0, -38.0, 0.0],   # 左嘴角
    [26.0, -38.0, 0.0],    # 右嘴角
], dtype=np.float64)


def head_pose(landmarks, w, h, fx):
    """由 5 个关键点估 yaw/pitch/roll（度）。失败返回 None。"""
    img_pts = np.array(landmarks, dtype=np.float64).reshape(5, 2)
    cam = np.array([[fx, 0, w / 2.0], [0, fx, h / 2.0], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((4, 1))
    ok, rvec, tvec = cv2.solvePnP(FACE_3D, img_pts, cam, dist, flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        return None
    rmat, _ = cv2.Rodrigues(rvec)
    proj = np.hstack((rmat, tvec))
    _, _, _, _, _, _, euler = cv2.decomposeProjectionMatrix(proj)
    pitch, yaw, roll = [float(x) for x in euler.flatten()]
    return yaw, pitch, roll


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", default="auto", help="摄像头节点，auto=自动探测 UVC 编号")
    ap.add_argument("--model", default="/root/models/face_detection_yunet_2023mar.onnx")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--hfov", type=float, default=60.0, help="相机水平视场角（度），用于像素->角度")
    ap.add_argument("--score", type=float, default=0.7)
    ap.add_argument("--kp", type=float, default=0.55)
    ap.add_argument("--deadzone", type=float, default=5.0)
    ap.add_argument("--ema", type=float, default=0.35)
    ap.add_argument("--armed", action="store_true", help="真正驱动机械臂（默认只预览）")
    ap.add_argument("--pan", action="store_true", help="跟随底座")
    ap.add_argument("--tilt", action="store_true", help="跟随灯头俯仰")
    ap.add_argument("--elbow", action="store_true",
                    help="小臂参与俯仰（慢速粗调，灯头仍做快速细调）")
    ap.add_argument("--elbow-sign", type=int, default=1, choices=[1, -1],
                    help="小臂方向：+1 = 读数增大是低头（与 --tilt-sign 同向）")
    ap.add_argument("--elbow-rate", type=float, default=0.5,
                    help="小臂接管速度（1/s，0.5 ≈ 2 秒到 63%%）")
    ap.add_argument("--elbow-max", type=float, default=25.0,
                    help="小臂最多接管多少度（安全上限）")
    ap.add_argument("--total-max", type=float, default=45.0,
                    help="俯仰累计修正上限（度）")
    ap.add_argument("--yaw-max", type=float, default=40.0,
                    help="底座累计修正上限（度）")
    ap.add_argument("--pan-sign", type=int, default=1, choices=[1, -1])
    ap.add_argument("--tilt-sign", type=int, default=1, choices=[1, -1])
    ap.add_argument("--max-step", type=int, default=25, help="每帧最大 tick 增量")
    ap.add_argument("--span", type=float, default=30.0,
                    help="相对起点的活动范围（度），机械限位未标定时的安全围栏")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--log", default="/tmp/face_follow.jsonl")
    ap.add_argument("--sec", type=float, default=0, help="运行秒数，0=一直跑")
    args = ap.parse_args()

    if not (args.pan or args.tilt or args.elbow):
        args.tilt = True   # 默认只动俯仰

    cam = Camera(args.dev, args.width, args.height, args.fps)
    W, H = cam.w, cam.h
    fx = (W / 2.0) / math.tan(math.radians(args.hfov / 2.0))
    print("摄像头 %dx%d  fx=%.1f (hfov=%.0f°)" % (W, H, fx, args.hfov))

    det = cv2.FaceDetectorYN.create(args.model, "", (W, H), args.score, 0.3, 5000)
    ctrl = FollowController(args.kp, args.deadzone, args.ema,
                            elbow_rate=args.elbow_rate, elbow_max=args.elbow_max,
                            total_max=args.total_max, yaw_max=args.yaw_max)

    arm = Arm(args.armed, args.pan, args.tilt, use_elbow=args.elbow,
              max_step=args.max_step, span_deg=args.span)
    if args.armed:
        arm.open()
        print("机械臂: pan=%s tilt=%s elbow=%s 范围=%s"
              % (arm.use_pan, arm.use_tilt, arm.use_elbow, arm.envelope))
        if arm.errors:
            print("!! 有舵机带错误，相关轴已禁用: %s" % arm.errors)

    preview = Preview()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(preview))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("预览: http://<本机IP>:%d/" % args.port)

    logf = open(args.log, "a", buffering=1) if args.log else None
    t0 = time.time()
    n = 0
    fps_t = t0
    fps = 0.0
    last_ctrl = 0.0

    try:
        while True:
            ok, frame = cam.read()
            if not ok:
                time.sleep(0.01)
                continue
            n += 1
            now = time.time()
            if now - fps_t >= 1.0:
                fps = n / (now - fps_t)
                n = 0
                fps_t = now

            _, faces = det.detect(frame)

            found = faces is not None and len(faces) > 0
            cx = cy = fw = fh = score = 0.0
            yaw = pitch = roll = 0.0
            err_yaw = err_pitch = 0.0
            vis = frame.copy()

            # 画面中心十字
            cv2.line(vis, (W // 2 - 30, H // 2), (W // 2 + 30, H // 2), (90, 90, 90), 1)
            cv2.line(vis, (W // 2, H // 2 - 30), (W // 2, H // 2 + 30), (90, 90, 90), 1)

            if found:
                f = max(faces, key=lambda r: float(r[2]) * float(r[3]))
                x, y, w_, h_ = float(f[0]), float(f[1]), float(f[2]), float(f[3])
                score = float(f[-1])
                cx, cy = x + w_ / 2.0, y + h_ / 2.0
                fw, fh = w_, h_
                err_yaw = math.degrees(math.atan((cx - W / 2.0) / fx))
                err_pitch = math.degrees(math.atan((cy - H / 2.0) / fx))
                lm = f[4:14].reshape(5, 2)
                hp = head_pose(lm, W, H, fx)
                if hp:
                    yaw, pitch, roll = hp

                cv2.rectangle(vis, (int(x), int(y)), (int(x + w_), int(y + h_)), (0, 220, 0), 2)
                for p in lm:
                    cv2.circle(vis, (int(p[0]), int(p[1])), 2, (0, 200, 255), -1)
                cv2.line(vis, (W // 2, H // 2), (int(cx), int(cy)), (80, 160, 255), 1)

            pan_abs, tilt_abs, elb_abs = ctrl.update(err_yaw, err_pitch, found)

            # 控制频率限制到 ~12Hz，别每帧都写舵机
            if args.armed and now - last_ctrl > 0.08:
                arm.step(pan_abs * args.pan_sign,
                         tilt_abs * args.tilt_sign,
                         elb_abs * args.elbow_sign)
                last_ctrl = now

            info = ("%s  fps=%.1f  err_yaw=%+.1f err_pitch=%+.1f  "
                    "head yaw=%+.0f pitch=%+.0f roll=%+.0f  "
                    "pan=%s tilt=%s elb=%s"
                    % ("FACE" if found else "----", fps, err_yaw, err_pitch,
                       yaw, pitch, roll,
                       arm.cmd.get(arm.PAN_ID, "-"), arm.cmd.get(arm.TILT_ID, "-"),
                       arm.cmd.get(arm.ELBOW_ID, "-")))
            cv2.putText(vis, info, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
            cv2.putText(vis, "armed=%s pan=%s tilt=%s elbow=%s"
                        % (args.armed, arm.use_pan, arm.use_tilt, arm.use_elbow),
                        (8, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 0), 1)

            okj, jpg = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if okj:
                preview.set(jpg.tobytes(), {
                    "found": bool(found), "fps": _f(fps, 1),
                    "err_yaw": _f(err_yaw), "err_pitch": _f(err_pitch),
                    "yaw": _f(yaw, 1), "pitch": _f(pitch, 1), "roll": _f(roll, 1),
                    "face_w": _f(fw, 1), "score": _f(score),
                    "pan": arm.cmd.get(arm.PAN_ID), "tilt": arm.cmd.get(arm.TILT_ID),
                    "elbow": arm.cmd.get(arm.ELBOW_ID),
                    "elbow_slow": _f(ctrl.slow, 1),
                    "armed": bool(args.armed), "pan_on": bool(arm.use_pan),
                    "tilt_on": bool(arm.use_tilt), "elbow_on": bool(arm.use_elbow),
                })

            if logf:
                try:
                    logf.write(json.dumps({
                        "t": _f(now - t0, 3), "found": bool(found),
                        "cx": _f(cx, 1), "cy": _f(cy, 1), "fw": _f(fw, 1),
                        "score": _f(score),
                        "err_yaw": _f(err_yaw), "err_pitch": _f(err_pitch),
                        "yaw": _f(yaw, 1), "pitch": _f(pitch, 1), "roll": _f(roll, 1),
                        "pan": arm.cmd.get(arm.PAN_ID), "tilt": arm.cmd.get(arm.TILT_ID),
                        "elbow": arm.cmd.get(arm.ELBOW_ID),
                        "slow": _f(ctrl.slow, 1),
                    }, ensure_ascii=False) + "\n")
                except Exception as e:
                    # 单帧写日志失败绝不能弄死整个跟随循环
                    pass

            if args.sec and now - t0 > args.sec:
                break
    except KeyboardInterrupt:
        pass
    finally:
        cam.close()
        arm.release()
        if logf:
            logf.close()
        srv.shutdown()
        print("已退出")


if __name__ == "__main__":
    main()

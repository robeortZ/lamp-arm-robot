#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台灯机械臂控制 — LeRobot SO-101 风格 5 舵机（无夹爪，末端改成灯头）

舵机总线: Feetech STS3215 (scservo_sdk)，/dev/serial/by-id/usb-1a86_USB_Single_Serial_*-if00

用法:
  arm_ctl.py scan                     扫描总线上有哪些舵机
  arm_ctl.py read                     读当前 5 个舵机位置
  arm_ctl.py goto --pose 1=2048,2=1800 [--ms 800] [--dry-run]
  arm_ctl.py home [--ms 1000]         回保存的基准位
  arm_ctl.py savehome                 把当前位置存成基准位
  arm_ctl.py action nod|shake|lookup|wave [--amp 10]
  arm_ctl.py torque on|off            使能/释放力矩（off 后可手动掰）

位置单位是舵机原始 tick（0-4095，中位 2048）。
"""

import argparse
import glob
import json
import os
import sys
import time

try:
    from scservo_sdk import (
        PortHandler,
        PacketHandler,
        COMM_SUCCESS,
    )
except ImportError:
    sys.exit("缺少 scservo_sdk，请先: /root/armenv/bin/pip install feetech-servo-sdk")

# ---------------------------------------------------------------- 常量

BAUD_CANDIDATES = [1000000, 500000, 115200]
PROTOCOL = 0

ADDR_ID = 5
ADDR_BAUD = 6
ADDR_OFFSET = 31         # Homing_Offset，有符号 16 位，写它=改"0 度"在哪
ADDR_TORQUE_ENABLE = 40
ADDR_GOAL_POSITION = 42
ADDR_GOAL_SPEED = 46
ADDR_PRESENT_POSITION = 56
ADDR_PRESENT_VOLTAGE = 62
ADDR_PRESENT_TEMP = 63
ADDR_LOCK = 55          # EEPROM 锁，写 ID/波特率前必须置 0

# LeRobot SO-101 命名；本机不含夹爪，末端关节驱动灯头
SERVO_NAMES = {
    1: "shoulder_pan",
    2: "shoulder_lift",
    3: "elbow_flex",
    4: "wrist_flex",
    5: "wrist_roll(lamp)",
    6: "spare",
}

POSE_FILE = "/etc/arm-pose.json"
LIMIT_FILE = "/etc/arm-limits.json"
CALIB_FILE = "/etc/arm-calibration.json"

# lerobot 的 so_follower 把 wrist_roll 当全周轴（不量限位，直接 0~4095）。
# 但本机实测 ID5 是「大行程 + 有限位」，不是全周，所以不套那个惯例。
FULL_TURN_IDS = set()

# 编码器一圈 4096；4096/360 ≈ 11.38 tick 每度
TICK_PER_DEG = 4096.0 / 360.0

# 可跨 0/4095 连续旋转的关节。ID1 底座实测能从 4056 直接到 33，
# 说明没有机械死点，插值必须走"跨圈最短路径"，否则会白转一整圈。
# 做完中位校准后底座不会再压在回绕点上，届时把 wrap_ids 置空即可。
CONF_FILE = "/etc/arm-config.json"


def load_wrap_ids():
    if os.path.exists(CONF_FILE):
        try:
            return set(json.load(open(CONF_FILE)).get("wrap_ids", [1]))
        except Exception:
            pass
    return {1}


WRAP_IDS = load_wrap_ids()

# 保守限位：只允许在各自范围内动，避免掰坏结构。可改 /etc/arm-limits.json 覆盖
# 限位：先给全量程，真实安全范围等实测后写 /etc/arm-limits.json 覆盖。
# 千万别用拍脑袋的窄区间——clamp 到一个和实际姿态无关的范围会让舵机猛跳到边界。
DEFAULT_LIMITS = {
    1: [0, 4095],
    2: [0, 4095],
    3: [0, 4095],
    4: [0, 4095],
    5: [0, 4095],
}


# ---------------------------------------------------------------- 底层

def resolve_port(q):
    if q and q != "auto":
        return q
    cands = sorted(glob.glob("/dev/serial/by-id/usb-1a86_*Serial*-if00"))
    if not cands:
        cands = sorted(glob.glob("/dev/ttyACM*"))
    if not cands:
        sys.exit("找不到舵机串口（检查 USB 线和供电）")
    return cands[0]


class Bus:
    def __init__(self, port="auto", baud=None):
        self.port_name = resolve_port(port)
        self.port = PortHandler(self.port_name)
        if not self.port.openPort():
            sys.exit("打开串口失败: %s" % self.port_name)

        ok = False
        for b in ([baud] if baud else BAUD_CANDIDATES):
            if self.port.setBaudRate(b):
                self.packet = PacketHandler(PROTOCOL)
                if self._anyone_alive():
                    self.baud = b
                    ok = True
                    break
        if not ok:
            self.port.closePort()
            sys.exit("波特率都不通（试过 %s），检查供电和接线" % BAUD_CANDIDATES)

    # -- 基础读写
    def ping(self, sid):
        model, comm, err = self.packet.ping(self.port, sid)
        if comm == COMM_SUCCESS and err == 0:
            return model
        return None

    def _anyone_alive(self):
        for sid in range(1, 7):
            if self.ping(sid) is not None:
                return True
        return False

    def read_pos(self, sid):
        # 不用 SDK 的 read2ByteTxRx：它在本板这条链路上偶发返回
        # comm=SUCCESS 但 data 为空，内部直接 IndexError 把整个流程打断。
        for _ in range(3):
            d, c, e = self.packet.readTxRx(self.port, sid, ADDR_PRESENT_POSITION, 2)
            if c == COMM_SUCCESS and d is not None and len(d) >= 2:
                return (d[0] | (d[1] << 8)) if e == 0 else None
            time.sleep(0.02)
        return None

    def read_all(self, ids):
        out = {}
        for sid in ids:
            p = self.read_pos(sid)
            if p is not None:
                out[sid] = p
        return out

    def set_torque(self, sid, on):
        self.packet.write1ByteTxRx(self.port, sid, ADDR_TORQUE_ENABLE, 1 if on else 0)

    def write_goal(self, sid, pos, speed=0):
        # 只写目标位置，不碰力矩开关。
        # 出厂/上次掉电后 GOAL_POSITION 常残留 0，若"先开力矩再写目标"，
        # 舵机会先朝 0 猛冲一下（0 和实际姿态差几千 tick），极易崩齿。
        # 开力矩的正确做法见 arm_grip()：先把目标锁成当前位置，再使能。
        self.packet.write2ByteTxRx(self.port, sid, ADDR_GOAL_POSITION, int(pos))
        if speed:
            self.packet.write2ByteTxRx(self.port, sid, ADDR_GOAL_SPEED, int(speed))

    def close(self):
        try:
            self.port.closePort()
        except Exception:
            pass


# ---------------------------------------------------------------- 工具

def load_limits():
    lim = dict(DEFAULT_LIMITS)
    if os.path.exists(LIMIT_FILE):
        try:
            raw = json.load(open(LIMIT_FILE))
            for k, v in raw.items():
                lim[int(k)] = v
        except Exception as e:
            print("读取 %s 失败，用默认限位: %s" % (LIMIT_FILE, e))
    return lim


def clamp(pose, limits):
    out = {}
    for sid, pos in pose.items():
        lo, hi = limits.get(sid, [0, 4095])
        if pos < lo or pos > hi:
            print("  ! 舵机 %d 目标 %d 超限 [%d,%d]，已夹紧" % (sid, pos, lo, hi))
        out[sid] = max(lo, min(hi, int(pos)))
    return out


ERR_BITS = {
    1: "电压异常", 2: "传感器异常", 4: "过温", 8: "过流/过载",
    16: "角度超限", 32: "过载",
}


def decode_err(e):
    if not e:
        return "正常"
    return ",".join(v for k, v in ERR_BITS.items() if e & k) or ("err=%d" % e)


def check_servo_errors(bus, ids, release=True):
    """读每个舵机的错误标志位。有错的立刻释放该轴力矩，返回 {id: err}。"""
    bad = {}
    for s in ids:
        d, c, e = bus.packet.readTxRx(bus.port, s, ADDR_PRESENT_POSITION, 2)
        if c == COMM_SUCCESS and e:
            bad[s] = e
    if bad and release:
        for s in bad:
            bus.set_torque(s, False)
        print("  ! 这些舵机报错，已释放其力矩: %s"
              % ", ".join("ID%d(%s)" % (s, decode_err(e)) for s, e in bad.items()))
    return bad


def arm_grip(bus, ids):
    """安全使能力矩：先把每个舵机的目标位置锁存成它自己的当前位置，再开力矩。
    这样上电/使能瞬间机械臂纹丝不动。返回使能前的位置。"""
    # 已经处于错误状态的轴不要再去驱动，否则会二次堵转
    pre = check_servo_errors(bus, list(ids), release=False)
    if pre:
        print("  ! 这些舵机当前带错误，先跳过: %s"
              % ", ".join("ID%d(%s)" % (s, decode_err(e)) for s, e in pre.items()))
        ids = [i for i in ids if i not in pre]
    if not ids:
        print("  ! 没有可用舵机")
        return {}

    cur = bus.read_all(ids)
    if not cur:
        print("读不到当前位置，不敢使能力矩")
        return {}
    for s, p in cur.items():
        bus.write_goal(s, p)
    time.sleep(0.05)
    for s in cur:
        bus.set_torque(s, True)
    time.sleep(0.12)

    # 使能后立刻查错误位：堵转/过流的轴必须马上放掉，不能让它硬顶
    bad = check_servo_errors(bus, list(cur.keys()))
    if bad:
        return {}

    after = bus.read_all(cur.keys())
    drift = {s: after.get(s, p) - p for s, p in cur.items() if abs(after.get(s, p) - p) > 60}
    if drift:
        print("  ! 使能后位置漂移较大（可能被重力带了一下）: %s" % drift)
    return after


def wrap_delta(cur, goal):
    """跨圈最短路径：从 cur 走到 goal 需要多少 tick（可正可负，绝对值 <= 2048）。"""
    return ((goal - cur + 2048) % 4096) - 2048


def smooth_goto(bus, target, ms=800, hz=50, dry=False, grip=True):
    """线性插值平滑移动；返回实际到位位置"""
    cur = bus.read_all(target.keys())
    if not cur:
        print("读不到当前位置，放弃移动")
        return {}
    missing = [s for s in target if s not in cur]
    if missing:
        print("  ! 这些舵机没响应，跳过: %s" % missing)
    target = {s: v for s, v in target.items() if s in cur}
    if not target:
        return {}

    def delta(s):
        d = target[s] - cur[s]
        if s in WRAP_IDS:
            d = wrap_delta(cur[s], target[s])
        return d

    steps = max(1, int(ms / 1000.0 * hz))
    if dry:
        for s in target:
            print("  dry-run: %d %d -> %d  (实际走 %+d tick)" % (s, cur[s], target[s], delta(s)))
        return target

    big = {s: abs(delta(s)) for s in target if abs(delta(s)) > 600}
    if big:
        print("  ! 单次跨度较大（>600 tick ≈ 53°）: %s，动作会慢一些" % big)
        steps = max(steps, int(steps * 1.5))

    if grip:
        # 关键：先把目标锁成当前位置，再使能力矩，避免朝残留的 0 位置冲
        g = arm_grip(bus, list(target.keys()))
        if not g:
            print("  ! 使能力矩失败（见上面的错误信息），放弃本次移动")
            return {}
        cur = g

    for i in range(1, steps + 1):
        t = i / float(steps)
        # 缓入缓出
        e = t * t * (3 - 2 * t)
        for s in target:
            v = cur[s] + delta(s) * e
            bus.write_goal(s, v % 4096 if s in WRAP_IDS else v)
        # 每 0.5 秒查一次错误位：中途堵转/过载要立刻停，不能一路顶到底
        if i % max(1, int(hz / 2)) == 0:
            bad = check_servo_errors(bus, list(target.keys()))
            if bad:
                print("  ! 移动过程中出现过载/错误，已中止本段动作")
                return {}
        time.sleep(1.0 / hz)
    return target


def fmt(pose):
    return ", ".join("%s(%d)=%d" % (SERVO_NAMES.get(s, "?"), s, p) for s, p in sorted(pose.items()))


# ---------------------------------------------------------------- 命令

def cmd_scan(args):
    bus = Bus(args.port, args.baud)
    print("端口 %s  波特率 %d" % (bus.port_name, bus.baud))
    found = []
    for sid in range(0, 21):
        model = bus.ping(sid)
        if model is not None:
            pos = bus.read_pos(sid)
            vol = temp = None
            v, c, e = bus.packet.read1ByteTxRx(bus.port, sid, ADDR_PRESENT_VOLTAGE)
            if c == COMM_SUCCESS and e == 0:
                vol = v / 10.0
            t, c, e = bus.packet.read1ByteTxRx(bus.port, sid, ADDR_PRESENT_TEMP)
            if c == COMM_SUCCESS and e == 0:
                temp = t
            name = SERVO_NAMES.get(sid, "-")
            print("  ID %-2d  %-18s model=%-5d pos=%-5s 电压=%sV 温度=%s℃"
                  % (sid, name, model, pos,
                     ("%.1f" % vol) if vol is not None else "?",
                     temp if temp is not None else "?"))
            found.append(sid)
    bus.close()
    if not found:
        print("没有舵机响应")
        return 1
    print("共 %d 个舵机: %s" % (len(found), found))
    return 0


def cmd_read(args):
    bus = Bus(args.port, args.baud)
    ids = sorted(SERVO_NAMES.keys())
    errs = check_servo_errors(bus, ids, release=False)
    pose = bus.read_all(ids)
    bus.close()
    print(fmt(pose))
    if errs:
        print("错误: " + ", ".join("ID%d(%s)" % (s, decode_err(e)) for s, e in errs.items()))
    print(json.dumps(pose))
    return 0


def cmd_goto(args):
    pose = {}
    for part in args.pose.split(","):
        part = part.strip()
        if not part:
            continue
        k, v = part.split("=")
        pose[int(k)] = int(v)
    limits = load_limits()
    target = clamp(pose, limits)
    bus = Bus(args.port, args.baud)
    print("目标:", fmt(target))
    smooth_goto(bus, target, ms=args.ms, dry=args.dry_run)
    time.sleep(0.3)
    print("到位:", fmt(bus.read_all(target.keys())))
    bus.close()
    return 0


def cmd_savehome(args):
    bus = Bus(args.port, args.baud)
    pose = bus.read_all(sorted(SERVO_NAMES.keys()))
    bus.close()
    if not pose:
        print("读不到位置")
        return 1
    data = {"base_pose": {str(k): v for k, v in pose.items()},
            "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    old = {}
    if os.path.exists(POSE_FILE):
        try:
            old = json.load(open(POSE_FILE))
        except Exception:
            old = {}
    old.update(data)
    json.dump(old, open(POSE_FILE, "w"), indent=2, ensure_ascii=False)
    print("已存 %s: %s" % (POSE_FILE, fmt(pose)))
    return 0


def cmd_home(args):
    if not os.path.exists(POSE_FILE):
        print("还没有基准位，先摆好姿态后执行: arm_ctl.py savehome")
        return 1
    cfg = json.load(open(POSE_FILE))
    base = {int(k): int(v) for k, v in cfg.get("base_pose", {}).items()}
    if not base:
        print("%s 里没有 base_pose" % POSE_FILE)
        return 1
    bus = Bus(args.port, args.baud)
    print("回基准位:", fmt(base))
    smooth_goto(bus, base, ms=args.ms, dry=args.dry_run)
    bus.close()
    return 0


def cmd_torque(args):
    bus = Bus(args.port, args.baud)
    if args.ids:
        want = [int(x) for x in args.ids.split(",")]
        alive = [sid for sid in range(1, 7) if sid in want and bus.ping(sid) is not None]
    else:
        alive = [sid for sid in range(1, 7) if bus.ping(sid) is not None]
    if args.state == "on":
        # 走 arm_grip，先把目标锁成当前位置再使能，避免朝残留 0 位置冲
        arm_grip(bus, alive)
    else:
        for sid in alive:
            bus.set_torque(sid, False)
    bus.close()
    print("力矩已%s（%s）" % ("使能，已锁住，别硬掰" if args.state == "on" else "释放",
                             ",".join(map(str, alive))))
    return 0


def cmd_action(args):
    """台灯形态的小动作。关节语义已由手掰实测确认：
    1=底座旋转 2=大臂俯仰 3=小臂弯折 4=灯头俯仰 5=灯头自转
    """
    if not os.path.exists(POSE_FILE):
        print("先 savehome 存基准位")
        return 1
    base = {int(k): int(v) for k, v in json.load(open(POSE_FILE)).get("base_pose", {}).items()}
    if not base:
        print("没有 base_pose")
        return 1

    amp = int(args.amp * TICK_PER_DEG)   # 幅度按"度"传入
    limits = load_limits()
    bus = Bus(args.port, args.baud)
    alive = [i for i in sorted(base) if bus.ping(i) is not None]
    if not alive:
        bus.close()
        print("舵机都不在线")
        return 1
    # 只在开头使能一次；后面每步不再重复使能，动作才连贯
    if not arm_grip(bus, alive):
        bus.close()
        print("使能力矩失败，动作取消")
        return 1

    def go(mod, ms=400):
        tgt = dict(base)
        tgt.update(mod)
        tgt = clamp(tgt, limits)
        smooth_goto(bus, tgt, ms=ms, grip=False)

    if args.name == "nod":
        # 灯头点头：灯头俯仰轴来回（对称摆动，不依赖正负方向定义）
        j = 4
        c = base.get(j, 2048)
        for _ in range(args.times):
            go({j: c - amp}, 240)
            go({j: c + amp}, 240)
        go({j: c}, 260)
    elif args.name == "shake":
        # 摇头：灯头自转轴左右摆（底座不动，整条手臂不会跟着甩）
        j = 5
        c = base.get(j, 2048)
        for _ in range(args.times):
            go({j: c - amp}, 240)
            go({j: c + amp}, 240)
        go({j: c}, 260)
    elif args.name == "lookup":
        # 抬头：灯头俯仰往上抬
        j = 4
        c = base.get(j, 2048)
        go({j: c + amp}, 600)
    elif args.name == "lookdown":
        # 低头：灯头俯仰往下压
        j = 4
        c = base.get(j, 2048)
        go({j: c - amp}, 600)
    elif args.name == "wave":
        # 挥灯：底座左右扫（跨圈安全），像招手
        j = 1
        c = base.get(j, 2048)
        for _ in range(args.times):
            go({j: (c - amp) % 4096}, 260)
            go({j: (c + amp) % 4096}, 260)
        go({j: c}, 300)
    elif args.name == "reset":
        go({}, int(args.ms))
    else:
        print("未知动作", args.name)
        bus.close()
        return 1
    bus.close()
    return 0


def cmd_dump(args):
    """把 0~63 号寄存器整块读出来解码。纯读，不写。"""
    bus = Bus(args.port, args.baud)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else [1, 2, 3, 4, 5]
    for sid in ids:
        data, comm, err = bus.packet.readTxRx(bus.port, sid, 0, 64)
        if comm != COMM_SUCCESS:
            print("ID %d 读失败 comm=%s err=%s" % (sid, comm, err))
            continue
        b = list(data)
        w = lambda a: b[a] | (b[a + 1] << 8)
        print("=== ID %d ===" % sid)
        print("  固件            %d.%d / 型号 %d.%d" % (b[0], b[1], b[3], b[4]))
        print("  波特率档位      %d" % b[6])
        print("  EEPROM 限位     min=%d max=%d" % (w(9), w(11)))
        print("  最高温/压       %d℃ / %.1fV ~ %.1fV"
              % (b[13], b[14] / 10.0, b[15] / 10.0))
        print("  最大力矩        %d" % w(16))
        print("  PID             P=%d D=%d I=%d" % (b[21], b[22], b[23]))
        print("  死区            CW=%d CCW=%d" % (b[26], b[27]))
        print("  中位偏移        %d" % w(31))
        print("  模式            %d  (0=位置伺服 1=电机 2=PWM 3=步进)" % b[33])
        print("  力矩开关        %d" % b[40])
        print("  目标            位置=%d 时间=%d 速度=%d"
              % (w(42), w(44), w(46)))
        print("  当前            位置=%d 速度=%d 负载=%d"
              % (w(56), w(58), w(60)))
        print("  供电/温度       %.1fV / %d℃" % (b[62] / 10.0, b[63]))
        print("  原始字节:")
        for row in range(0, 64, 8):
            print("    [%2d] " % row + " ".join("%02x" % x for x in b[row:row + 8]))
    bus.close()
    return 0


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def _read2(bus, sid, addr):
    """按 2 字节读一个寄存器。不用 SDK 的 read2ByteTxRx：
    它在某些地址上会返回 comm=SUCCESS 但 data 为空，内部直接 IndexError。
    这里自己拼，并做 3 次重试。"""
    for _ in range(3):
        d, c, e = bus.packet.readTxRx(bus.port, sid, addr, 2)
        if c == COMM_SUCCESS and d is not None and len(d) >= 2:
            return d[0] | (d[1] << 8)
        time.sleep(0.02)
    return None


def _write2(bus, sid, addr, val):
    # 注意签名是 (port, id, address, data)，不是 (…, length, data)
    return bus.packet.write2ByteTxRx(bus.port, sid, addr, val & 0xFFFF)


def cmd_zero(args):
    """中位校准：把「现在这个姿态」定义成 2048（写 Homing_Offset 到 EEPROM）。

    力矩必须关闭。因为改 offset 会同时改变 GOAL_POSITION 的坐标系，
    使能状态下写 offset，舵机会立刻朝旧目标在新坐标系下的位置跑。
    """
    bus = Bus(args.port, args.baud)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else [1, 2, 3, 4, 5]
    alive = [i for i in ids if bus.ping(i) is not None]
    if not alive:
        bus.close()
        print("没有舵机响应")
        return 1

    on = []
    for i in alive:
        tq, _, _ = bus.packet.read1ByteTxRx(bus.port, i, ADDR_TORQUE_ENABLE)
        if tq == 1:
            on.append(i)
    if on and not args.force:
        bus.close()
        print("这些舵机力矩还开着: %s" % on)
        print("先执行: arm_ctl.py torque off")
        print("（改 offset 会连带改 GOAL 的坐标系，使能状态下会当场乱动）")
        return 1

    # 安全护栏：关节必须停在行程中段，不能在极限附近定中位。
    # 之前 ID3 就是掰在折叠极限上跑了 zero，写出的 offset 把行程切进了
    # 编码器 0/4095 回绕区，之后每次发目标都朝死点冲。
    near = []
    try:
        lim = json.load(open(LIMIT_FILE))
    except Exception:
        lim = {}
    for i in alive:
        p = bus.read_pos(i)
        if p is None:
            continue
        lo, hi = lim.get(str(i), [0, 4095])
        margin = max(100, int((hi - lo) * 0.08))
        if p - lo < margin or hi - p < margin:
            near.append((i, p, lo, hi))
    if near and not args.force:
        bus.close()
        for i, p, lo, hi in near:
            print("  ! ID %d 当前读数 %d，距限位 [%d, %d] 不到 %d tick"
                  % (i, p, lo, hi, margin))
        print("关节停在极限附近，不能在此姿态定中位。")
        print("请先把这个关节掰到行程中间，再执行 zero；确认无误可用 --force 跳过检查。")
        return 1

    target = args.to
    for i in alive:
        # 1) 第一次用探测法确定 offset 与读数的符号/比例关系，避免瞎加减
        p0 = bus.read_pos(i)
        o0 = _s16(_read2(bus, i, ADDR_OFFSET) or 0)
        bus.packet.write1ByteTxRx(bus.port, i, ADDR_LOCK, 0)
        _write2(bus, i, ADDR_OFFSET, o0 + 50)
        time.sleep(0.03)
        p1 = bus.read_pos(i)
        _write2(bus, i, ADDR_OFFSET, o0)
        time.sleep(0.03)

        slope = 1.0
        if p0 is not None and p1 is not None and abs(p1 - p0) > 5:
            slope = (p1 - p0) / 50.0

        # 2) 按探测到的斜率一次算准，再迭代收尾
        cur_o = o0
        for _ in range(3):
            p = bus.read_pos(i)
            if p is None:
                break
            err = target - p
            if abs(err) <= 2:
                break
            cur_o = _s16(cur_o + int(round(err / slope)))
            bus.packet.write1ByteTxRx(bus.port, i, ADDR_LOCK, 0)
            time.sleep(0.01)
            _write2(bus, i, ADDR_OFFSET, cur_o)
            bus.packet.write1ByteTxRx(bus.port, i, ADDR_LOCK, 1)
            time.sleep(0.05)

        p2 = bus.read_pos(i)
        print("  ID %d %-18s 原读数 %-5s offset %+6d -> %+6d  新读数 %s"
              % (i, SERVO_NAMES.get(i, "-"), p0, o0, cur_o, p2))
    bus.close()
    print('\n中位已写死。注意把 /etc/arm-config.json 里的 "wrap_ids" 清空：'
          '校准后底座不再压在回绕点上了。')
    return 0


def cmd_limits(args):
    """极限行程校准：你把每个关节推到两端，这里记录 min/max 并写限位文件。"""
    bus = Bus(args.port, args.baud)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else [1, 2, 3, 4, 5]
    alive = [i for i in ids if bus.ping(i) is not None]
    if not alive:
        bus.close()
        print("没有舵机响应")
        return 1
    print("开始记录 %.0fs。请把每个关节慢慢推到两个方向的尽头（各停 2 秒再换下一个）。" % args.sec)
    # 跨圈感知：不取 min/max（穿过 0/4095 时会被骗成"整圈"），
    # 而是累加相邻采样点的最短路径增量，得到相对起点的真实位移。
    start, prev, acc = {}, {}, {}
    amin, amax = {}, {}
    t0 = time.time()
    try:
        while time.time() - t0 < args.sec:
            for i in alive:
                p = bus.read_pos(i)
                if p is None:
                    continue
                if i not in start:
                    start[i] = p
                    prev[i] = p
                    acc[i] = 0
                    amin[i] = 0
                    amax[i] = 0
                else:
                    acc[i] += wrap_delta(prev[i], p)
                    prev[i] = p
                    amin[i] = min(amin[i], acc[i])
                    amax[i] = max(amax[i], acc[i])
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass

    # 收尾时趁串口还开着，把每个舵机当前的 Homing_Offset 读出来，导 lerobot 格式要用
    homing = {}
    for i in alive:
        v = _read2(bus, i, ADDR_OFFSET)
        homing[i] = _s16(v) if v is not None else 0
    bus.close()

    lim, rng, wrap_note = {}, {}, {}
    print("\n记录结果（行程按路径累加，可跨 0/4095）:")
    for i in sorted(start):
        s = start[i]
        lo_u = s + amin[i]          # 未折返坐标，可能 <0 或 >4095
        hi_u = s + amax[i]
        span = hi_u - lo_u
        lo = lo_u % 4096
        hi = hi_u % 4096
        wraps = (lo_u < 0) or (hi_u > 4095)
        if span < 20:
            # 关节根本没被推动（或者只抖动了几下）：不能当成"零宽度限位"存盘，
            # 否则这个轴会被 clamp 死在一个点上。
            wrap_note[i] = "几乎没动（跨度 %d tick），本次忽略，不写限位" % span
            dn = dx = 0.0
            rng[str(i)] = {"start": s, "skipped": True, "span": span}
            print("  ID %d %-18s 起点%4d  跨度 %d tick  <== %s"
                  % (i, SERVO_NAMES.get(i, "-"), s, span, wrap_note[i]))
            continue
        if span >= 3900:
            wrap_note[i] = "全周轴，限位按 0~4095"
            lim[str(i)] = [0, 4095]
        elif wraps:
            wrap_note[i] = "行程跨 0/4095，不能用单区间表示，需重新中位后再测"
        else:
            lim[str(i)] = [lo, hi]
        dn = (lo - 2047) * 360.0 / 4096
        dx = (hi - 2047) * 360.0 / 4096
        rng[str(i)] = {"start": s, "min_raw": lo, "max_raw": hi, "span": span,
                       "wraps": wraps,
                       "min_deg": round(dn, 1), "max_deg": round(dx, 1),
                       "span_deg": round(span * 360.0 / 4096, 1)}
        print("  ID %d %-18s 起点%4d  终点 %4d ~ %4d  路径跨度 %4d tick = %5.1f°%s"
              % (i, SERVO_NAMES.get(i, "-"), s, lo, hi, span,
                 span * 360.0 / 4096,
                 ("  <== " + wrap_note[i]) if i in wrap_note else ""))

    if args.save:
        # 合并写：只测了部分关节时不要覆盖其他关节已有的值
        merged = {}
        if os.path.exists(LIMIT_FILE):
            try:
                merged = json.load(open(LIMIT_FILE))
            except Exception:
                merged = {}
        merged.update(lim)
        json.dump(merged, open(LIMIT_FILE, "w"), indent=2)
        rmerged = {}
        if os.path.exists("/etc/arm-range.json"):
            try:
                rmerged = json.load(open("/etc/arm-range.json"))
            except Exception:
                rmerged = {}
        rmerged.update(rng)
        json.dump(rmerged, open("/etc/arm-range.json", "w"), indent=2, ensure_ascii=False)
        print("\n已写 %s（合并后 %d 个关节）与 /etc/arm-range.json"
              % (LIMIT_FILE, len(merged)))

        # 再按 lerobot 的 MotorCalibration 格式导一份，方便以后接 lerobot 时直接导入
        cal = {}
        for k, v in merged.items():
            i = int(k)
            cal[SERVO_NAMES.get(i, "motor%d" % i).replace("(lamp)", "")] = {
                "id": i,
                "drive_mode": 0,
                "homing_offset": homing.get(i, 0),
                "range_min": 0 if i in FULL_TURN_IDS else v[0],
                "range_max": 4095 if i in FULL_TURN_IDS else v[1],
            }
        json.dump(cal, open(CALIB_FILE, "w"), indent=2, ensure_ascii=False)
        print("已写 %s（lerobot MotorCalibration 格式）" % CALIB_FILE)
    else:
        print("\n（未保存，加 --save 才会写文件）")
    return 0


def cmd_setid(args):
    """把总线上的某个舵机改成新 ID。要求旧 ID 上是唯一设备。"""
    bus = Bus(args.port, args.baud)
    old, new = args.old, args.new
    if old == new:
        bus.close()
        print("新旧 ID 相同，无需修改")
        return 0

    model = bus.ping(old)
    if model is None:
        # 再试一次，区分"没设备"和"撞号"
        comms = []
        for _ in range(4):
            m, c, e = bus.packet.ping(bus.port, old)
            comms.append(c)
            time.sleep(0.02)
        bus.close()
        if any(c == -7 for c in comms):
            print("ID %d 回包校验失败（RX_CORRUPT），总线上很可能有多个舵机撞在 ID %d。"
                  % (old, old))
            print("请先拔掉其中一个舵机的线（只留一个），再执行本命令。")
        else:
            print("ID %d 无响应，检查接线/供电" % old)
        return 1

    if bus.ping(new) is not None:
        bus.close()
        print("ID %d 已经有设备了，不能占用。先给目标 ID 上的舵机换个号。" % new)
        return 1

    print("找到 ID %d  model=%d，准备改为 ID %d" % (old, model, new))
    if args.dry_run:
        bus.close()
        print("[dry-run] 未写入")
        return 0

    # 不要在这里关力矩：如果舵机正吊着机械臂，一关就整条手臂砸下来。
    # 写 ID 只需要解开 EEPROM 锁，与力矩状态无关。
    tq, _, _ = bus.packet.read1ByteTxRx(bus.port, old, ADDR_TORQUE_ENABLE)
    print("当前力矩=%s（保持不动，只解 EEPROM 锁）" % tq)
    c1, e1 = bus.packet.write1ByteTxRx(bus.port, old, ADDR_LOCK, 0)
    time.sleep(0.02)
    c2, e2 = bus.packet.write1ByteTxRx(bus.port, old, ADDR_ID, new)
    time.sleep(0.1)
    bus.close()

    if c2 != COMM_SUCCESS or e2 != 0:
        print("写 ID 失败 comm=%s err=%s（EEPROM 解锁 comm=%s err=%s）" % (c2, e2, c1, e1))
        return 1

    bus2 = Bus(args.port, args.baud)
    ok = bus2.ping(new) is not None
    bus2.close()
    if ok:
        print("成功：ID %d -> %d（已重新上电验证）" % (old, new))
        return 0
    print("写入后探测不到 ID %d，请给舵机断电重上电后再 scan" % new)
    return 1


def cmd_watch(args):
    """实时打印若干舵机的当前位置，最后汇总「谁动了、动了多少」。

    用途：力矩 off 的状态下手掰某一个关节，看哪个 ID 跟着变，
    从而客观确认 ID <-> 物理关节的对应关系（不靠猜）。
    """
    bus = Bus(args.port, args.baud)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else [1, 2, 3, 4, 5]
    alive = [i for i in ids if bus.ping(i) is not None]
    if not alive:
        bus.close()
        print("没有舵机响应")
        return 1
    print("监听 %s，%.0fs" % (alive, args.sec))
    hdr = "  ".join("ID%d" % i for i in alive)
    print("  时间  " + hdr)

    lo = {i: None for i in alive}
    hi = {i: None for i in alive}
    last = {}
    t0 = time.time()
    try:
        while time.time() - t0 < args.sec:
            row, changed = [], []
            for i in alive:
                p = bus.read_pos(i)
                row.append("%5s" % ("-" if p is None else p))
                if p is None:
                    continue
                lo[i] = p if lo[i] is None else min(lo[i], p)
                hi[i] = p if hi[i] is None else max(hi[i], p)
                if i not in last or abs(p - last[i]) >= args.delta:
                    changed.append(i)
                last[i] = p
            # 只有发生变化才打印，日志好读
            if changed or not last:
                print("%6.1fs  %s   <- %s" % (time.time() - t0, "  ".join(row),
                                              ",".join(map(str, changed))))
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    bus.close()

    print("\n汇总（行程由大到小）:")
    rows = []
    for i in alive:
        if lo[i] is None:
            continue
        rows.append((hi[i] - lo[i], i, lo[i], hi[i]))
    rows.sort(reverse=True)
    for span, i, a, b in rows:
        flag = "  <== 动过" if span >= args.delta else ""
        print("  ID %d %-18s %4d ~ %4d  行程 %4d%s"
              % (i, SERVO_NAMES.get(i, "-"), a, b, span, flag))
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="台灯机械臂控制")
    ap.add_argument("--port", default="auto", help="串口，默认自动找 by-id")
    ap.add_argument("--baud", type=int, default=None, help="波特率，默认自动试")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan")
    sub.add_parser("read")

    p = sub.add_parser("goto")
    p.add_argument("--pose", required=True, help="例如 1=2048,2=1800")
    p.add_argument("--ms", type=int, default=800)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("home")
    p.add_argument("--ms", type=int, default=1000)
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("savehome")

    p = sub.add_parser("torque")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--ids", default=None, help="例如 2,3,4,5，默认全部")

    p = sub.add_parser("action")
    p.add_argument("name", choices=["nod", "shake", "lookup", "lookdown", "wave", "reset"])
    p.add_argument("--amp", type=float, default=8.0, help="幅度（度），默认 8")
    p.add_argument("--times", type=int, default=2, help="来回次数，默认 2")
    p.add_argument("--ms", type=int, default=800, help="reset 用：回位耗时")

    p = sub.add_parser("setid")
    p.add_argument("old", type=int)
    p.add_argument("new", type=int)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("dump")
    p.add_argument("--ids", default=None, help="例如 3,4,5，默认 1,2,3,4,5")

    p = sub.add_parser("zero")
    p.add_argument("--ids", default=None, help="默认全部 5 个")
    p.add_argument("--to", type=int, default=2047, help="目标中位，默认 2047（与 lerobot 一致）")
    p.add_argument("--force", action="store_true", help="力矩开着也硬写（危险）")

    p = sub.add_parser("limits")
    p.add_argument("--ids", default=None)
    p.add_argument("--sec", type=float, default=120.0)
    p.add_argument("--save", action="store_true", help="写入限位文件")

    p = sub.add_parser("watch")
    p.add_argument("--ids", default=None, help="例如 3,4,5，默认 1,2,3,4,5")
    p.add_argument("--sec", type=float, default=15.0)
    p.add_argument("--interval", type=float, default=0.2)
    p.add_argument("--delta", type=int, default=8, help="变化超过多少 tick 才打印")

    args = ap.parse_args()
    fn = {
        "scan": cmd_scan, "read": cmd_read, "goto": cmd_goto,
        "home": cmd_home, "savehome": cmd_savehome,
        "torque": cmd_torque, "action": cmd_action,
        "setid": cmd_setid, "watch": cmd_watch, "dump": cmd_dump,
        "zero": cmd_zero, "limits": cmd_limits,
    }[args.cmd]
    sys.exit(fn(args) or 0)


if __name__ == "__main__":
    main()

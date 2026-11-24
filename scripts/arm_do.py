#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arm_do.py — 单次执行一个「表情级」机械臂动作（端侧 MCP 工具 / 命令行共用）

用法
----
    python3 /root/arm_do.py nod            # 点头（"对啊 / 好的"）
    python3 /root/arm_do.py shake_head     # 摇摇头（"不对 / 不知道"）
    python3 /root/arm_do.py wave           # 招手
    python3 /root/arm_do.py look_up        # 抬头
    python3 /root/arm_do.py list           # 列出可用动作

    --dry-run     只打印要做什么，不连机械臂
    --keep-up     做完停在迎宾基准位并保持力矩（默认做完收回纳位并松力矩）
    --port / --robot-id / --base-pose   同其它脚本（默认 auto：按 /dev/serial/by-id 找臂，
                                         不受 ttyACM 编号漂移影响；--base-pose 默认迎宾基准位）

两条执行路径（自动选）
----------------------
1. **迎宾模式在跑** ⇒ 走它的控制口 `/run/vision-greet.sock`，让它顺手做一个动作。
   好处：不用停服务、不用重启（重启要约 10 秒，而且灯头会先收起再抬起，很突兀），
   动作还会排进它自己的动作队列，不抢串口。
2. **没在跑** ⇒ 本脚本自己连机械臂：
   连臂 → 转到迎宾基准位 → 做动作 → 收回纳位 → 松力矩 → 退出。
   若这时是「人脸跟随」占着串口，会先停掉它，做完再把跟随拉起来。

⚠️ /dev/ttyACM0 是独占的：两个进程同时连必然有一个失败，所以上面才会先看控制口。

惰性导入
--------
lerobot / torch / yolov8pose… 这一串 import 要 5~10 秒。
本脚本的重活只有「路径 2」才需要，所以全部压到函数里：
  * 路径 1（控制口）→ 纯 socket，几十毫秒返回；
  * `list` / `--dry-run` → 任何 python3 都能跑，不用 conda 环境也能查用法。
导入顺序也照 vision_interact.py 的坑处理：**先 torch/lerobot，再 rknnlite**，
否则 rknnlite 会把 logging 的级别表洗坏，torch 一 import 就崩。
"""
import argparse
import json
import os
import socket
import subprocess
import sys

sys.path.insert(0, '/root')

BASE_POSE_PATH = '/etc/greet-pose.json'
GREET_SOCK = '/run/vision-greet.sock'
FOLLOW_UNIT = 'face-follow'

# CH343（1a86:55d3）每次断电重连都会重新枚举：ttyACM0 -> ttyACM1 -> ……，编号会漂移。
# 用 by-id 的稳定路径，别写死 ttyACM0（写死会在重连后报「Missing motor IDs ... found {}」，
# 看着像没供电，其实只是找错了节点）。
ARM_BY_ID = '/dev/serial/by-id/usb-1a86_USB_Single_Serial_5AB0179621-if00'


def resolve_arm_port(preferred='auto'):
    """把 --port 解析成真实设备路径（显式给了存在的路径就尊重它）"""
    import glob
    if preferred and preferred != 'auto' and os.path.exists(preferred):
        return preferred
    if os.path.exists(ARM_BY_ID):
        return ARM_BY_ID
    cands = []
    for p in glob.glob('/dev/ttyACM*'):
        try:
            cands.append((int(p.replace('/dev/ttyACM', '')), p))
        except ValueError:
            pass
    if cands:
        return sorted(cands)[0][1]
    return preferred if (preferred and preferred != 'auto') else '/dev/ttyACM0'

# 动作名 -> (arm_actions 里的函数名, 中文名)
ACTIONS = {
    'nod':        ('nod',        '点头'),
    'shake_head': ('shake_head', '摇摇头'),
    'wave':       ('wave',       '招手'),
    'look_up':    ('look_up',    '抬头'),
}


# ---------------------------------------------------------------- 惰性导入
def _load_arm():
    """导入动作库（会连带 torch/lerobot，慢；只在真要动机械臂时调）"""
    # 顺序要紧：先让 torch/lerobot 把 logging 建好，再进带 rknnlite 的模块
    import lerobot.robots.so101_follower.so101_follower  # noqa: F401
    import arm_actions
    return arm_actions


def _home_pose():
    """HOME 姿态（读不到就返回 None，由调用方给明确报错）"""
    try:
        from yolov8pose_head_follow_so101 import HOME_POSE_DEG
        return dict(HOME_POSE_DEG)
    except Exception as e:
        print('[arm_do] 读 HOME_POSE_DEG 失败: %r' % e, file=sys.stderr, flush=True)
        return None


# ---------------------------------------------------------------- 控制口
def try_greet_socket(action, timeout=2.0):
    """迎宾服务在跑就让它做。

    返回 (状态, 消息)，状态：
      'ok'      已交给它执行
      'state'   查询状态（回的是 state: 开头的一行）
      'busy'    它在忙（机械臂正被动作队列占用）——别自己抢串口
      'unknown' 它不认识这个动作（大概是旧版服务）——回退到直连
      'absent'  控制口不存在 / 连不上 ——回退到直连
    """
    if not os.path.exists(GREET_SOCK):
        return 'absent', '控制口不存在（迎宾服务没在跑）'
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(GREET_SOCK)
        s.sendall((action + '\n').encode('utf-8'))
        buf = b''
        while b'\n' not in buf and len(buf) < 512:
            chunk = s.recv(128)
            if not chunk:
                break
            buf += chunk
        reply = buf.decode('utf-8', 'replace').strip()
    except Exception as e:
        return 'absent', '控制口连不上（%r）' % e
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    if reply.startswith('ok'):
        return 'ok', reply
    if reply.startswith('state'):
        return 'state', reply
    if reply.startswith('busy'):
        return 'busy', reply
    if reply.startswith('unknown'):
        return 'unknown', reply
    return 'absent', '控制口回了看不懂的内容：%r' % reply


# ---------------------------------------------------------------- 直连
def unit_active(unit):
    try:
        return subprocess.run(['systemctl', 'is-active', '--quiet', unit],
                              timeout=5).returncode == 0
    except Exception:
        return False


def load_base_pose(path=BASE_POSE_PATH):
    """读迎宾基准位（arm_pose_save.py 写的 base_pose），读不到退回 HOME"""
    home = _home_pose()

    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return (dict(home) if home else None,
                '没有 %s，用 HOME 姿态' % path)
    except Exception as e:
        return (dict(home) if home else None,
                '读 %s 失败(%r)，用 HOME 姿态' % (path, e))

    pose = raw.get('base_pose') if isinstance(raw, dict) else None
    if not isinstance(pose, dict):
        return (dict(home) if home else None,
                '%s 里没有 base_pose，用 HOME 姿态' % path)

    base = dict(home) if home else {}
    got = 0
    for k, v in pose.items():
        if isinstance(v, (int, float)):
            base[k] = float(v)
            got += 1
    if got == 0:
        return (dict(home) if home else None,
                'base_pose 没有有效数值，用 HOME 姿态')
    return base, '用 %s 里的基座位' % path


def do_direct(action, opt):
    """自己连机械臂做一次：基准位 -> 动作 -> 收纳 -> 松力矩"""
    base, note = load_base_pose(opt.base_pose)
    if not base:
        print('[arm_do] 拿不到基准位，没法动机械臂；'
              '请用 --base-pose 指定，或先跑 arm_pose_save.py', file=sys.stderr)
        return 5
    print('[arm_do] 基准位：%s' % note, flush=True)

    arm = _load_arm()
    fn = getattr(arm, ACTIONS[action][0])

    from lerobot.robots.so101_follower.config_so101_follower import SO101FollowerConfig
    from lerobot.robots.so101_follower.so101_follower import SO101Follower

    robot = None
    restored_follow = False
    try:
        if unit_active(FOLLOW_UNIT):
            print('[arm_do] 人脸跟随占着串口，先停它（做完会拉起来）', flush=True)
            subprocess.run(['systemctl', 'stop', FOLLOW_UNIT], timeout=25)
            restored_follow = True
            import time
            time.sleep(1.5)

        cfg = SO101FollowerConfig(port=opt.port, id=opt.robot_id,
                                  cameras={}, use_degrees=True)
        robot = SO101Follower(cfg)
        robot.connect()
        names = list(robot.action_features.keys())
        print('[arm_do] 已连接 %s' % opt.port, flush=True)

        arm.goto(robot, names, base, duration=1.6)     # 先站到基准位，动作才看得见
        fn(robot, names, base)
        print('[arm_do] %s 完成' % ACTIONS[action][1], flush=True)

        if opt.keep_up:
            arm.goto(robot, names, base, duration=0.8)
            robot.bus.disconnect(disable_torque=False)
            print('[arm_do] 停在基准位并保持力矩', flush=True)
        else:
            arm.goto_park(robot, names)
            robot.bus.disconnect(disable_torque=True)
            print('[arm_do] 已收回纳位并松力矩', flush=True)
        return 0
    except Exception as e:
        print('[arm_do] 失败: %r' % e, file=sys.stderr, flush=True)
        if robot is not None:
            try:
                robot.bus.disconnect(disable_torque=True)
            except Exception:
                pass
        return 3
    finally:
        if restored_follow:
            try:
                subprocess.run(['systemctl', 'start', FOLLOW_UNIT], timeout=25)
                print('[arm_do] 已恢复人脸跟随', flush=True)
            except Exception as e:
                print('[arm_do] 恢复人脸跟随失败: %r' % e, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description='单次执行一个机械臂小动作')
    ap.add_argument('action', nargs='?', default='list')
    ap.add_argument('--port', default='auto',
                    help='机械臂串口；auto=优先 /dev/serial/by-id（编号漂移也不用管）')
    ap.add_argument('--robot-id', default='my_awesome_follower_arm')
    ap.add_argument('--base-pose', default=BASE_POSE_PATH)
    ap.add_argument('--dry-run', action='store_true', help='只打印，不连机械臂')
    ap.add_argument('--keep-up', action='store_true',
                    help='做完停在基准位并保持力矩（默认收回纳位并松力矩）')
    opt = ap.parse_args()

    if opt.action in ('list', 'help'):
        print('可用动作：')
        for k, (_fn, cn) in sorted(ACTIONS.items()):
            print('  %-11s %s' % (k, cn))
        return 0

    if opt.action not in ACTIONS:
        print('未知动作 %r；可用：%s' % (opt.action, ', '.join(sorted(ACTIONS))))
        return 2

    label = ACTIONS[opt.action][1]

    if opt.dry_run:
        print('[arm_do] dry-run：会做「%s」（不连机械臂）' % label)
        return 0

    state, info = try_greet_socket(opt.action)
    if state == 'ok':
        print('[arm_do] 迎宾在跑，已交给它执行「%s」：%s' % (label, info), flush=True)
        return 0
    if state == 'busy':
        print('[arm_do] 机械臂正忙，这次没做「%s」：%s' % (label, info),
              file=sys.stderr, flush=True)
        return 4

    print('[arm_do] %s，自己连机械臂做「%s」' % (info, label), flush=True)
    opt.port = resolve_arm_port(opt.port)
    return do_direct(opt.action, opt)


if __name__ == '__main__':
    sys.exit(main())

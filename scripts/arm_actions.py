#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arm_actions.py — SO-101 机械臂「表情级」小动作库

设计原则（安全第一）：
  * 只用小幅度（默认 ±12~22°）、每一步都走 smooth_goto 插值，绝不跳跃发指令；
  * 每个动作结束都回到传入的 base 姿态（默认 HOME），不会让机械臂停在奇怪位置；
  * 收纳统一走 PARK_POSE_DEG（和跟随脚本 Ctrl+C 的归位点完全一致）。

关节索引（lerobot 名称）：
  shoulder_pan 水平旋转 / shoulder_lift 大臂抬降 / elbow_flex 肘
  wrist_flex 腕俯仰（点头方向） / wrist_roll 腕自转（摆手方向） / gripper 夹爪
"""
import time

from yolov8pose_head_follow_so101 import (build_pose, smooth_goto,
                                          HOME_POSE_DEG, PARK_POSE_DEG)

PAN = 'shoulder_pan.pos'
LIFT = 'shoulder_lift.pos'
ELBOW = 'elbow_flex.pos'
WFLEX = 'wrist_flex.pos'
WROLL = 'wrist_roll.pos'
GRIP = 'gripper.pos'


def _with(base, deltas):
    """在 base 姿态上叠加增量（不改原字典）"""
    d = dict(base)
    for k, v in deltas.items():
        d[k] = d.get(k, 0.0) + v
    return d


def goto(robot, names, pose_deg, duration=2.0, steps=40):
    return smooth_goto(robot, names, build_pose(pose_deg, names),
                       duration=duration, steps=steps)


def goto_home(robot, names, duration=2.5, steps=40):
    return goto(robot, names, HOME_POSE_DEG, duration, steps)


def goto_park(robot, names, duration=3.0, steps=50):
    return goto(robot, names, PARK_POSE_DEG, duration, steps)


def nod(robot, names, base=None, amp=13.0, cycles=2, step_s=0.26, steps=8):
    """点头：wrist_flex 下-上小幅摆动 cycles 次"""
    base = base or HOME_POSE_DEG
    for _ in range(cycles):
        smooth_goto(robot, names,
                    build_pose(_with(base, {WFLEX: amp}), names), step_s, steps)
        smooth_goto(robot, names,
                    build_pose(_with(base, {WFLEX: -amp * 0.6}), names), step_s, steps)
    return goto(robot, names, base, step_s, steps)


def wave(robot, names, base=None, amp=20.0, cycles=3, step_s=0.24, steps=8):
    """招手：wrist_roll 左右摆动（像摆手打招呼）"""
    base = base or HOME_POSE_DEG
    for _ in range(cycles):
        smooth_goto(robot, names,
                    build_pose(_with(base, {WROLL: amp}), names), step_s, steps)
        smooth_goto(robot, names,
                    build_pose(_with(base, {WROLL: -amp}), names), step_s, steps)
    return goto(robot, names, base, step_s, steps)


def look_up(robot, names, base=None, lift=14.0, wflex=-7.0, duration=0.9, steps=20):
    """抬头（表示"注意到你了"）"""
    base = base or HOME_POSE_DEG
    return goto(robot, names,
                _with(base, {LIFT: lift, WFLEX: wflex}), duration, steps)


def turn_to(robot, names, base=None, pan=18.0, duration=1.0, steps=24):
    """朝一侧转（告别时"目送"）"""
    base = base or HOME_POSE_DEG
    return goto(robot, names, _with(base, {PAN: pan}), duration, steps)


def pan_wiggle(robot, names, base=None, amp=16.0, cycles=2, step_s=0.34, steps=12):
    """左右小幅旋转：绕 shoulder_pan 在基准姿态上左右摆动

    和「人脸跟随」那种大范围扫视不同，迎宾只是"左右看看有没有人"，
    所以幅度刻意小（默认 ±16°）、速度慢，并且每次都插值到位、最后回到 base，
    不会甩臂、也不会把臂留在歪的位置。
    """
    base = base or HOME_POSE_DEG
    for _ in range(cycles):
        smooth_goto(robot, names,
                    build_pose(_with(base, {PAN: amp}), names), step_s, steps)
        smooth_goto(robot, names,
                    build_pose(_with(base, {PAN: -amp}), names), step_s, steps)
    return goto(robot, names, base, step_s, steps)


def shake_head(robot, names, base=None, amp=10.0, cycles=2, step_s=0.20, steps=8):
    """摇摇头（"不对 / 不知道"）：**只转灯头**，其它关节不动

    只绕 shoulder_pan（5 号舵机）在基准姿态上左右各拧一下，
    其余关节每一步都写回 base，所以看上去是灯头在摇头，不是整条臂在扫。

    和 pan_wiggle 是同一个自由度，但语义和手感不同：
      * pan_wiggle = "左右看看有没有人"：慢（0.34s/步）、幅度大（±16°）；
      * shake_head = "摇头说不"：快（0.20s/步）、幅度小（±10°）、来回更干脆。

    仍然是每一步都走 smooth_goto 插值，最后回到 base，不会甩臂。
    """
    return pan_wiggle(robot, names, base=base, amp=amp, cycles=cycles,
                      step_s=step_s, steps=steps)


def gripper_wiggle(robot, names, base=None, delta=16.0, cycles=2, step_s=0.22, steps=6):
    """夹爪开合几下（俏皮的小招呼）"""
    base = base or HOME_POSE_DEG
    for _ in range(cycles):
        smooth_goto(robot, names,
                    build_pose(_with(base, {GRIP: delta}), names), step_s, steps)
        smooth_goto(robot, names,
                    build_pose(_with(base, {GRIP: -delta}), names), step_s, steps)
    return goto(robot, names, base, step_s, steps)

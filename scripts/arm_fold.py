#!/root/armenv/bin/python
# -*- coding: utf-8 -*-
"""arm_fold.py — 收起机械臂（安全版）

流程：
  1. torque on（arm_grip 先锁当前位置再使能，不冲位）
  2. smooth_goto 回 /etc/arm-pose.json 的 base_pose（收纳位）
  3. 轮询实际位置反馈，直到所有关节与目标差 <= TOL（或超时 TIMEOUT 秒）
  4. 确认到位后才 torque off
  5. 输出一行 JSON 结果（供 MCP 工具解析）

用法：arm_fold.py [--ms 1500] [--tol 30] [--timeout 8]
"""
import sys
import os
import json
import time

sys.path.insert(0, "/usr/local/sbin")
import arm_ctl as A

def main():
    argv = sys.argv[1:]
    ms = 1500
    tol = 30
    timeout = 8.0
    if "--ms" in argv:
        ms = int(argv[argv.index("--ms") + 1])
    if "--tol" in argv:
        tol = int(argv[argv.index("--tol") + 1])
    if "--timeout" in argv:
        timeout = float(argv[argv.index("--timeout") + 1])

    result = {"ok": False, "moved": False, "released": False, "positions": {}}

    # 1. 收纳位：优先 /etc/arm-fold-pose.json（真正的收拢姿势），
    #    没有才退回 /etc/arm-pose.json 的 base_pose（迎宾伸展位，卸力后会塌）
    pose_file = None
    for f in ("/etc/arm-fold-pose.json", A.POSE_FILE):
        if os.path.exists(f):
            pose_file = f
            break
    if pose_file is None:
        result["reason"] = "no fold/home pose file"
        print(json.dumps(result, ensure_ascii=False))
        return 1
    try:
        cfg = json.load(open(pose_file))
        base = {int(k): int(v) for k, v in cfg.get("base_pose", {}).items()}
    except Exception as e:
        result["reason"] = "bad pose file: %s" % e
        print(json.dumps(result, ensure_ascii=False))
        return 1
    if not base:
        result["reason"] = "no base_pose"
        print(json.dumps(result, ensure_ascii=False))
        return 1
    result["pose_file"] = pose_file

    bus = A.Bus("auto", None)
    try:
        ids = sorted(base.keys())

        # 2. 锁位使能（先读当前位置再上电，避免冲位）
        grip = A.arm_grip(bus, ids)
        if not grip:
            result["reason"] = "torque enable failed"
            print(json.dumps(result, ensure_ascii=False))
            return 1

        # 3. 平滑运动到收纳位
        A.smooth_goto(bus, base, ms=ms, grip=False)
        result["moved"] = True
        t0 = time.time()

        # 4. 位置反馈轮询：所有关节到位（含 wrap 关节）才算数
        def reached(cur):
            for s in ids:
                if s not in cur:
                    return False
                d = cur[s] - base[s]
                if s in getattr(A, "WRAP_IDS", ()):
                    d = A.wrap_delta(cur[s], base[s])
                if abs(d) > tol:
                    return False
            return True

        deadline = time.time() + timeout
        cur = {}
        while time.time() < deadline:
            cur = bus.read_all(ids) or {}
            if cur and reached(cur):
                break
            time.sleep(0.15)

        settled = bool(cur) and reached(cur)
        # 到位后稳定一下再卸力
        time.sleep(0.3)

        # 5. 到位确认后才卸力
        alive = [sid for sid in ids if bus.ping(sid) is not None]
        for sid in alive:
            bus.set_torque(sid, False)
        result["released"] = True
        result["ok"] = settled
        result["settled"] = settled
        result["elapsed"] = round(time.time() - t0, 2)
        if not settled:
            result["reason"] = "timeout waiting arrival, released anyway at closest position"

        final = bus.read_all(ids) or {}
        result["positions"] = {str(k): v for k, v in final.items()}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if settled else 2
    finally:
        bus.close()

if __name__ == "__main__":
    sys.exit(main())

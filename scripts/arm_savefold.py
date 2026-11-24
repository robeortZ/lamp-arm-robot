#!/root/armenv/bin/python
# -*- coding: utf-8 -*-
"""arm_savefold.py — 把机械臂当前位置保存为收纳位（/etc/arm-fold-pose.json）

用法：先把机械臂掰到想要的收纳姿势（力矩释放状态手掰即可），
然后执行 arm_savefold.py。之后「收起机械臂」就会先运动到这个姿势再卸力。
"""
import sys
import json
import time

sys.path.insert(0, "/usr/local/sbin")
import arm_ctl as A

FOLD_FILE = "/etc/arm-fold-pose.json"

def main():
    bus = A.Bus("auto", None)
    try:
        ids = sorted(A.SERVO_NAMES.keys())
        pose = bus.read_all(ids)
        if not pose:
            print(json.dumps({"ok": False, "reason": "read failed"}, ensure_ascii=False))
            return 1
        data = {"base_pose": {str(k): v for k, v in pose.items()},
                "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
        json.dump(data, open(FOLD_FILE, "w"), indent=2, ensure_ascii=False)
        print(json.dumps({"ok": True, "file": FOLD_FILE,
                          "positions": {str(k): v for k, v in pose.items()}},
                         ensure_ascii=False))
        return 0
    finally:
        bus.close()

if __name__ == "__main__":
    sys.exit(main())

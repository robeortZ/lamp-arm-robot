#!/root/armenv/bin/python
# -*- coding: utf-8 -*-
"""arm_relax.py — 纯卸力：全舵机释放力矩（不移动任何关节）
输出一行 JSON：{"ok":true,"released":[1,2,3,4,5]}
注意：卸力后机械臂会被自重压塌，属正常现象（配合「收起机械臂」可先归位再卸力）。
"""
import sys
import json
sys.path.insert(0, "/usr/local/sbin")
import arm_ctl as A

bus = A.Bus("auto", None)
ids = [i for i in (1, 2, 3, 4, 5) if bus.ping(i) is not None]
ok = []
for s in ids:
    try:
        bus.set_torque(s, False)
        ok.append(s)
    except Exception:
        pass
bus.close()
print(json.dumps({"ok": True, "released": ok}))

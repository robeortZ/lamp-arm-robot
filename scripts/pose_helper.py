#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中位摆位辅助：实时显示 5 个关节当前位置，以及离「机械行程中点」还差多少。

目标值来自 /etc/arm-range.json（上一次 recorded 的 min/max）。
调到位以后再跑 arm_ctl.py zero 把中点写成 2047。

浏览器打开 http://<板子IP>:8082/
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/usr/local/sbin")
import arm_ctl as A

NAMES = {1: "底座", 2: "大臂", 3: "小臂", 4: "灯头俯仰", 5: "灯头自转"}
RANGE_FILE = "/etc/arm-range.json"

state = {"pos": {}, "target": {}, "span": {}, "ts": 0.0, "err": ""}
lock = threading.Lock()


def load_targets():
    """从上次记录的行程推算中点。跨圈（跨度>3000）的关节不做推算。"""
    tgt, span = {}, {}
    try:
        raw = json.load(open(RANGE_FILE))
    except Exception as e:
        return tgt, span, "读不到 %s: %s" % (RANGE_FILE, e)
    for k, v in raw.items():
        i = int(k)
        lo, hi = v["min_raw"], v["max_raw"]
        span[i] = hi - lo
        if hi - lo > 3000:
            continue          # 跨圈/全周，中点无意义，留给人工
        tgt[i] = (lo + hi) // 2
    return tgt, span, ""


def reader():
    bus = A.Bus("auto")
    while True:
        for i in range(1, 6):
            p = bus.read_pos(i)
            if p is not None:
                with lock:
                    state["pos"][i] = p
        with lock:
            state["ts"] = time.time()
        time.sleep(0.1)


PAGE = """<!doctype html><meta charset=utf-8><title>中位摆位</title>
<style>
body{background:#14161a;color:#e6e6e6;font-family:system-ui,sans-serif;margin:0;padding:18px}
h2{margin:0 0 4px;font-size:18px}
p.tip{color:#9aa4b2;font-size:13px;margin:6px 0 14px}
table{border-collapse:collapse;width:100%;max-width:720px}
td,th{padding:9px 10px;border-bottom:1px solid #262b33;font-size:15px}
th{color:#8b95a3;font-weight:500;font-size:13px;text-align:left}
td.num{font-variant-numeric:tabular-nums;font-size:18px}
.ok{color:#3ddc84}.mid{color:#ffcc4d}.bad{color:#ff6b6b}.na{color:#5c6673}
</style>
<h2>中位摆位辅助</h2>
<p class="tip">把每个关节掰到「差值」接近 0（绿色）即可。差值 = 当前 - 行程中点。<br>
灯头俯仰请用肉眼摆到光轴水平（它的行程跨圈，中点算不出来）。</p>
<table id=t></table>
<script>
async function tick(){
  const r = await fetch('/pos'); const d = await r.json();
  let h='<tr><th>关节</th><th>当前</th><th>中点目标</th><th>差值</th><th>状态</th></tr>';
  for(const row of d.rows){
    let cls='na', txt='—';
    if(row.delta!==null){
      const a=Math.abs(row.delta);
      cls = a<=30?'ok':(a<=90?'mid':'bad');
      txt = (a<=30?'可以了':(row.delta>0?'偏大，往小掰':'偏小，往大掰'));
    }
    h+=`<tr><td>${row.id} ${row.name}</td><td class=num>${row.pos??'-'}</td>
        <td class=num>${row.target??'-'}</td>
        <td class="num ${cls}">${row.delta===null?'—':(row.delta>0?'+':'')+row.delta}</td>
        <td class="${cls}">${txt}</td></tr>`;
  }
  document.getElementById('t').innerHTML=h;
}
setInterval(tick,200); tick();
</script>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/pos"):
            with lock:
                pos, tgt, span = dict(state["pos"]), dict(state["target"]), dict(state["span"])
            rows = []
            for i in range(1, 6):
                p = pos.get(i)
                t = tgt.get(i)
                rows.append({
                    "id": i, "name": NAMES.get(i, ""), "pos": p, "target": t,
                    "delta": (p - t) if (p is not None and t is not None) else None,
                })
            body = json.dumps({"rows": rows, "err": state["err"]},
                              ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    tgt, span, err = load_targets()
    state["target"], state["span"], state["err"] = tgt, span, err
    print("目标中点:", tgt)
    print("行程跨度:", span)
    if err:
        print("!", err)
    threading.Thread(target=reader, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", 8082), H)
    srv.daemon_threads = True
    print("浏览器打开 http://<本机IP>:8082/")
    srv.serve_forever()


if __name__ == "__main__":
    main()

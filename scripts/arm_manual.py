#!/root/armenv/bin/python
# -*- coding: utf-8 -*-
"""arm_manual.py — 机械臂手动调节模式（软跟随 + 松手锁定 + 网页调参）

⚠️ 实验结论（2026-11-24）：STS3215 的 Max Torque(reg16) 与 PID(reg26-29) 均为
上电锁存的 EEPROM 值，运行时写入/使能重采样都不生效（行为级对比实验证实）。
所以本脚本的「软」完全靠跟随循环实现：
  HOLD(保持)    位置环锁定目标位（刚度=出厂EEPROM，满力矩）→ 松手后稳定
  FOLLOW(掰动)  触发条件：偏离 > move_th(灵敏度) 或 负载 > load_th(触发力)
                进入后每周期目标位=实际位置，误差趋零 → 手感轻
  松手          位置静止 → 冻结目标=当前位置 → 刚度恢复

每关节可调参数（网页 http://<板子IP>:8090，存 /etc/arm-manual-torque.json）：
  move_th  触发灵敏度：偏离多少 tick 判定手在掰（越小越灵敏）
  load_th  触发力：负载超过多少 % 判定手碰到了（一碰就软）

用法:
  arm_manual.py run                 前台常驻循环（systemd 用）
  arm_manual.py oneshot [--sec 10]  跑 N 秒后退出（测试）
"""
import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/usr/local/sbin")
try:
    from scservo_sdk import PortHandler, PacketHandler, COMM_SUCCESS, GroupSyncRead
except ImportError:
    sys.exit("缺少 scservo_sdk，请先: /root/armenv/bin/pip install feetech-servo-sdk")
import arm_ctl as A

ADDR_PRESENT_POSITION = 56
ADDR_PRESENT_LOAD = 58
ADDR_MAX_TORQUE = 16        # 上电锁存，运行时写入无效（实验证实），仅退出恢复用

TORQUE_CFG_FILE = "/etc/arm-manual-torque.json"
SERVO_NAMES = {
    1: "底座左右转",
    2: "大臂(承重最大)",
    3: "小臂",
    4: "腕部俯仰",
    5: "灯头自转",
}

# 每关节触发参数：move_th=tick, load_th=负载%
DEFAULT_CFG = {
    1: {"move_th": 8, "load_th": 15},
    2: {"move_th": 10, "load_th": 20},
    3: {"move_th": 10, "load_th": 20},
    4: {"move_th": 10, "load_th": 15},
    5: {"move_th": 8, "load_th": 15},
}

RUNNING = True


def _stop(signum, frame):
    global RUNNING
    RUNNING = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


# ------------------------------------------------------------ 配置
def load_cfg():
    try:
        raw = json.load(open(TORQUE_CFG_FILE))
        cfg = {}
        for k in DEFAULT_CFG:
            v = raw.get(str(k), DEFAULT_CFG[k])
            cfg[k] = {"move_th": int(v.get("move_th", 10)),
                      "load_th": float(v.get("load_th", 20))}
        return cfg
    except Exception:
        return {k: dict(v) for k, v in DEFAULT_CFG.items()}


def save_cfg(cfg):
    try:
        with open(TORQUE_CFG_FILE, "w") as fp:
            json.dump({str(k): v for k, v in cfg.items()}, fp, indent=1)
        return True
    except Exception as e:
        print("!! 保存配置失败: %s" % e)
        return False


# ------------------------------------------------------------ 底层
def read_load(bus, sid):
    d, c, e = bus.packet.readTxRx(bus.port, sid, ADDR_PRESENT_LOAD, 2)
    if c == COMM_SUCCESS and d is not None and len(d) >= 2 and e == 0:
        raw = d[0] | (d[1] << 8)
        v = raw & 0x7FF
        return -v if raw & 0x800 else v
    return None


def _dev(a, b, sid):
    if sid in A.WRAP_IDS:
        return A.wrap_delta(a, b)
    return a - b


# ------------------------------------------------------------ 网页
PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>机械臂手动模式 · 触发调参</title>
<style>
body{font-family:system-ui,'Microsoft YaHei',sans-serif;background:#f5f6f8;margin:0;padding:16px;color:#222}
h1{font-size:20px;margin:0 0 4px} .sub{color:#666;font-size:13px;margin-bottom:14px;line-height:1.6}
.card{background:#fff;border-radius:10px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h2{font-size:15px;margin:0 0 8px}
.row{display:flex;align-items:center;gap:8px;margin:6px 0}
.row label{width:110px;font-size:13px;color:#555}
.row input[type=range]{flex:1}
.row .val{width:70px;text-align:right;font-variant-numeric:tabular-nums;font-size:13px;font-weight:600}
.tag{font-size:11px;color:#888;margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}
th,td{padding:5px 6px;text-align:left;border-bottom:1px solid #eee}
.st-hold{color:#1a7f37;font-weight:600}.st-follow{color:#b45309;font-weight:600}.st-off{color:#c0c0c0}
</style></head><body>
<h1>机械臂手动模式 · 触发调参</h1>
<div class="sub">说明：STS3215 的力矩/PID 是上电锁存参数，运行时改不了（实验证实）；
「软」的感觉由跟随循环实现——<b>触发越灵敏，从锁住到变软越快</b>。滑块拖动立即生效并自动保存。</div>
<div id="cards"></div>
<div class="card"><h2>实时状态 <span class="tag">500ms 自动刷新</span></h2>
<table><tr><th>舵机</th><th>状态</th><th>位置</th><th>负载%</th></tr><tbody id="live"></tbody></table></div>
<script>
const NAMES={1:"底座左右转",2:"大臂(承重最大)",3:"小臂",4:"腕部俯仰",5:"灯头自转"};
async function apply(id,key,val){
  await fetch('/api/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:id,key:key,value:val})});
}
function build(cfg){
  const c=document.getElementById('cards');c.innerHTML='';
  for(const id of [1,2,3,4,5]){
    const d=cfg[id]||{move_th:10,load_th:20};
    const el=document.createElement('div');el.className='card';
    el.innerHTML=`<h2>${id}号 · ${NAMES[id]}</h2>
      <div class="row"><label>触发灵敏度<br><span class="tag">偏离 tick，越小越灵</span></label>
        <input type="range" id="m${id}" min="4" max="60" value="${d.move_th}">
        <span class="val" id="mv${id}">${d.move_th}</span></div>
      <div class="row"><label>触发力<br><span class="tag">负载 %，一碰就软</span></label>
        <input type="range" id="l${id}" min="5" max="50" value="${d.load_th}">
        <span class="val" id="lv${id}">${d.load_th}</span></div>`;
    c.appendChild(el);
    for(const [k,vid,vv] of [['move_th','m'+id,'mv'+id],['load_th','l'+id,'lv'+id]]){
      const inp=el.querySelector('#'+vid);
      inp.addEventListener('input',()=>{el.querySelector('#'+vv).textContent=inp.value;});
      inp.addEventListener('change',()=>apply(id,k,parseFloat(inp.value)));
    }
  }
}
async function refresh(){
  try{
    const r=await fetch('/api/state');const j=await r.json();
    if(!window._built){build(j.cfg);window._built=true;}
    const tb=document.getElementById('live');let html='';
    for(const id of [1,2,3,4,5]){
      const s=(j.live&&j.live[id])||{};
      const st=s.st==='hold'?'<span class="st-hold">保持</span>':s.st==='follow'?'<span class="st-follow">跟随</span>':'<span class="st-off">离线</span>';
      html+=`<tr><td>${id}号 ${NAMES[id]}</td><td>${st}</td><td>${s.pos??'-'}</td><td>${s.load??'-'}</td></tr>`;
    }
    tb.innerHTML=html;
  }catch(e){}
}
setInterval(refresh,500);refresh();
</script></body></html>"""


def start_web(port, shared):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            elif self.path == "/api/state":
                with shared["lock"]:
                    data = {"cfg": shared["cfg"], "live": shared["live"]}
                self._send(200, "application/json",
                           json.dumps(data).encode("utf-8"))
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self):
            if self.path != "/api/set":
                self._send(404, "text/plain", b"not found")
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n))
                sid = int(req["id"]); key = req["key"]; val = float(req["value"])
            except Exception:
                self._send(400, "application/json", b'{"ok":false,"err":"bad request"}')
                return
            limits = {"move_th": (4, 60), "load_th": (5, 50)}
            if sid not in DEFAULT_CFG or key not in limits:
                self._send(400, "application/json", b'{"ok":false,"err":"bad id/key"}')
                return
            lo, hi = limits[key]
            val = max(lo, min(hi, val))
            if key == "move_th":
                val = int(val)
            with shared["lock"]:
                shared["cfg"][sid][key] = val
                save_cfg(shared["cfg"])
            print("网页调参: ID%d %s -> %s" % (sid, key, val))
            self._send(200, "application/json", b'{"ok":true}')

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ------------------------------------------------------------ 主循环
def loop(bus, ids, limits, args, sec_limit=None, shared=None):
    t_start = time.time()
    lock = shared["lock"]

    grip = A.arm_grip(bus, ids)
    if not grip:
        print("!! 使能力矩失败，退出")
        return 1
    goals = dict(grip)
    state = {s: "hold" for s in ids}
    still_cnt = {s: 0 for s in ids}
    dead_cnt = {s: 0 for s in ids}
    prev = dict(grip)
    with lock:
        move_th = {s: shared["cfg"][s]["move_th"] for s in ids}
        load_th = {s: shared["cfg"][s]["load_th"] for s in ids}

    print("手动模式启动: HOLD 保持中（move_th=%s load_th=%s）"
          % ({s: move_th[s] for s in ids}, {s: load_th[s] for s in ids}))
    print("网页调参: http://<板子IP>:%d" % args.web_port)

    gsr = GroupSyncRead(bus.port, bus.packet, ADDR_PRESENT_POSITION, 2)
    for s in ids:
        gsr.addParam(s)

    def read_positions():
        if gsr.txRxPacket() != COMM_SUCCESS:
            return None
        out = {}
        for s in ids:
            if gsr.isAvailable(s, ADDR_PRESENT_POSITION, 2):
                out[s] = gsr.getData(s, ADDR_PRESENT_POSITION, 2)
        return out

    cycle = 0
    while RUNNING:
        now = time.time()
        if sec_limit is not None and now - t_start > sec_limit:
            print("oneshot 时间到，退出（保持当前位置锁定）")
            break

        # 网页参数热更新
        with lock:
            for s in ids:
                move_th[s] = shared["cfg"][s]["move_th"]
                load_th[s] = shared["cfg"][s]["load_th"]

        cur = read_positions()
        if not cur:
            time.sleep(0.03)
            continue

        for s in ids:
            pos = cur.get(s)
            if pos is None:
                dead_cnt[s] += 1
                if dead_cnt[s] > 100 and state[s] != "offline":
                    bus.set_torque(s, False)
                    state[s] = "offline"
                    print("ID%d 连续失联，已释放力矩" % s)
                continue
            dead_cnt[s] = 0

            if state[s] == "offline":
                bus.write_goal(s, pos)
                time.sleep(0.05)
                bus.set_torque(s, True)
                time.sleep(0.1)
                goals[s] = bus.read_pos(s) or pos
                state[s] = "hold"
                still_cnt[s] = 0
                prev[s] = goals[s]
                print("ID%d 恢复在线，重新锁定" % s)
                continue

            if state[s] == "hold":
                d = _dev(pos, goals[s], s)
                if abs(d) > move_th[s]:
                    state[s] = "follow"
                    still_cnt[s] = 0
                    print("ID%d 进入跟随 (偏离 %+d tick)" % (s, d))
                else:
                    ld = read_load(bus, s)
                    if ld is not None and abs(ld) / 10.0 > load_th[s]:
                        state[s] = "follow"
                        still_cnt[s] = 0
                        print("ID%d 进入跟随 (一碰就软: 负载 %.1f%%)"
                              % (s, abs(ld) / 10.0))
                prev[s] = pos

            if state[s] == "follow":
                bus.write_goal(s, pos)
                if abs(_dev(pos, prev[s], s)) <= args.still:
                    still_cnt[s] += 1
                else:
                    still_cnt[s] = 0
                if still_cnt[s] >= args.still_n:
                    goals[s] = pos
                    bus.write_goal(s, goals[s])
                    state[s] = "hold"
                    print("ID%d 松手锁定在 %d" % (s, pos))
                prev[s] = pos

        cycle += 1
        if cycle % 100 == 0:      # ~0.5s 刷网页状态
            live = {}
            for s in ids:
                if state[s] == "offline":
                    live[s] = {"st": "offline", "pos": None, "load": None}
                    continue
                ld = read_load(bus, s)
                live[s] = {"st": state[s], "pos": cur.get(s),
                           "load": None if ld is None else abs(ld) / 10.0}
            with lock:
                shared["live"] = live
            A.check_servo_errors(bus, ids)
        time.sleep(args.interval)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "oneshot"])
    ap.add_argument("--sec", type=float, default=10.0, help="oneshot 运行秒数")
    ap.add_argument("--port", default="auto")
    ap.add_argument("--web-port", type=int, default=8090, help="网页调参端口")
    ap.add_argument("--still", type=int, default=4)
    ap.add_argument("--still-n", type=int, default=12)
    ap.add_argument("--interval", type=float, default=0.004)
    args = ap.parse_args()

    bus = A.Bus(args.port, None)
    alive = [i for i in (1, 2, 3, 4, 5) if bus.ping(i) is not None]
    if not alive:
        print("!! 没有响应的舵机，退出")
        return 1
    limits = A.load_limits()
    print("在线舵机: %s (port=%s)" % (alive, bus.port_name))

    cfg = load_cfg()
    shared = {"cfg": cfg, "live": {}, "lock": threading.Lock()}
    if args.cmd == "run":
        srv = start_web(args.web_port, shared)
        print("网页调参已启动: 0.0.0.0:%d" % args.web_port)

    sec = args.sec if args.cmd == "oneshot" else None
    try:
        rc = loop(bus, alive, limits, args, sec_limit=sec, shared=shared)
    finally:
        # 恢复满力矩（EEPROM 若被外部改过，保底恢复默认）
        for s in alive:
            try:
                d, c, e = bus.packet.readTxRx(bus.port, s, ADDR_MAX_TORQUE, 2)
                if c == COMM_SUCCESS and d and len(d) >= 2 and e == 0:
                    ft = d[0] | (d[1] << 8)
                    if ft != 1000:
                        bus.packet.write2ByteTxRx(bus.port, s, ADDR_MAX_TORQUE, 1000)
                        print("ID%d Max Torque 恢复 1000 (原 %d)" % (s, ft))
            except Exception:
                pass
        print("手动模式退出（位置保持锁定，torque 仍使能）")
        bus.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())

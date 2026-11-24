#!/root/armenv/bin/python
# -*- coding: utf-8 -*-
"""wifi_setup.py — 台灯机器人 SoftAP 配网门户（思路A：ESP 风格配网）

原理：
  板子开一个 "LampBot-Setup" **无密码开放热点**（nmcli hotspot, 2.4G）
  + 劫持 DNS（所有域名→板子IP）+ 80 端口配网页
  → 手机连热点后自动弹/手动开 http://192.168.0.1
  → 选 WiFi + 输密码 → 板子切回 STA 连接 → 验证联网 → 成功退出
  → 失败则自动重开热点，手机重连后页面显示错误原因

用法:
  wifi_setup.py portal [--timeout 900] [--no-ap]   配网门户主程序（systemd 用）
  wifi_setup.py watchdog                            断网看门狗（断网自动拉起 portal）
  wifi_setup.py connect --ssid X --password Y       命令行直连（调试用）

⚠️ 注意：portal 运行期间板子 WiFi 处于 AP 模式，原来的 STA 连接会断开
（SSH 会掉，直到配网成功/超时退出后自动恢复）。
"""
import argparse
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AP_CON_NAME = "LampBot-Setup"
AP_SSID = "LampBot-Setup"
PORTAL_IP_DEFAULT = "192.168.0.1"   # 配网页地址（可 --portal-ip 改）
WIFI_IF = "wlp1s0"
STATUS_DIR = "/run/wifi-setup"
RUNNING = True


def _stop(signum, frame):
    global RUNNING
    RUNNING = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def sh(cmd, timeout=30):
    """执行 shell 命令，返回 (rc, 输出)"""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return 1, "timeout: %s" % e


def write_status(**kw):
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        kw["ts"] = time.strftime("%H:%M:%S")
        with open(os.path.join(STATUS_DIR, "status.json"), "w") as fp:
            json.dump(kw, fp)
    except Exception as e:
        print("!! 写状态失败: %s" % e)


def read_status():
    try:
        with open(os.path.join(STATUS_DIR, "status.json")) as fp:
            return json.load(fp)
    except Exception:
        return {"phase": "idle"}


def check_internet(timeout=3):
    """真联网检测（多个 204 端点任一通过即算）"""
    import urllib.request
    urls = [
        "http://connect.rom.miui.com/generate_204",
        "http://204.ustclug.org/generate_204",
        "http://www.gstatic.com/generate_204",
    ]
    for u in urls:
        try:
            req = urllib.request.Request(u, method="GET")
            urllib.request.urlopen(req, timeout=timeout)
            return True
        except urllib.error.HTTPError:
            return True        # 能收到 HTTP 响应（含 4xx）= 有网
        except Exception:
            continue
    return False


def wifi_ip():
    rc, out = sh("nmcli -t -f IP4.ADDRESS device show %s" % WIFI_IF)
    for line in out.splitlines():
        if line.startswith("IP4.ADDRESS"):
            return line.split(":", 2)[-1].split("/")[0]
    return "10.42.0.1"


def start_hotspot(portal_ip):
    """开**无密码开放热点**（手机免密直连），门户 IP 固定为 portal_ip。
    注：开放热点不写任何 security 属性（写了会报错）；属性用点分语法。"""
    sh("nmcli connection delete %s 2>/dev/null" % AP_CON_NAME)
    rc, out = sh("nmcli connection add type wifi ifname %s con-name %s "
                 "ssid %s 802-11-wireless.mode ap 802-11-wireless.band bg "
                 "ipv4.method shared ipv4.addresses %s/24"
                 % (WIFI_IF, AP_CON_NAME, AP_SSID, portal_ip), timeout=30)
    if rc != 0:
        print("!! 建热点连接失败: %s" % out.strip()[:200])
        return None
    rc, out = sh("nmcli connection up %s" % AP_CON_NAME, timeout=45)
    if rc != 0:
        print("!! 开热点失败: %s" % out.strip()[:200])
        return None
    ip = wifi_ip()
    print("热点已开启: SSID=%s（无密码，开放） 板子IP=%s" % (AP_SSID, ip))
    return ip


def stop_hotspot():
    sh("nmcli connection down %s 2>/dev/null" % AP_CON_NAME)
    sh("nmcli connection delete %s 2>/dev/null" % AP_CON_NAME)


def scan_wifi():
    rc, out = sh("nmcli -t -f SSID,SIGNAL,SECURITY device wifi list ifname %s"
                 % WIFI_IF, timeout=20)
    seen = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        ssid, sig, sec = parts[0], parts[1], ":".join(parts[2:])
        if ssid and (ssid not in [s[0] for s in seen]):
            seen.append((ssid, int(sig) if sig.isdigit() else 0, sec))
    seen.sort(key=lambda x: -x[1])
    return [{"ssid": s, "signal": g,
             "sec": "锁" if sec and sec != "NONE" else "开放"}
            for s, g, sec in seen[:12]]


def try_connect(ssid, password):
    """停热点 → 等目标 SSID 出现 → 连 STA → 验证联网；失败重试/重开热点。返回错误信息或 None"""
    print("收到配网请求: ssid=%s" % ssid)
    write_status(phase="connecting", ssid=ssid)
    stop_hotspot()
    time.sleep(1.5)
    # [FIX-SCAN] AP 切回 STA 后信道要重置，主动扫描并等目标 SSID 出现（最多 ~20s）
    for i in range(6):
        rc, out = sh('nmcli -t -f SSID device wifi list ifname %s' % WIFI_IF,
                     timeout=15)
        if any(l.split(":")[0] == ssid for l in out.splitlines()):
            print("目标 SSID 已出现（第 %d 轮扫描）" % (i + 1))
            break
        sh("nmcli device wifi rescan ifname %s" % WIFI_IF, timeout=10)
        time.sleep(2)
    cmd = 'nmcli device wifi connect "%s" password "%s" ifname %s' \
          % (ssid, password.replace('"', ''), WIFI_IF)
    rc, out = sh(cmd, timeout=45)
    if rc != 0:
        # [FIX-SCAN] 重试一次（首轮扫描缓存不准时常见）
        time.sleep(3)
        rc, out = sh(cmd, timeout=45)
    if rc != 0:
        err = out.strip().splitlines()[-1][:160] if out.strip() else "连接失败"
        print("!! 连接失败: %s" % err)
        write_status(phase="failed", ssid=ssid, error=err)
        return err
    # 联网验证（最多 20s）
    for i in range(10):
        if check_internet(3):
            print("配网成功: %s 已联网" % ssid)
            write_status(phase="connected", ssid=ssid)
            return None
        time.sleep(2)
    err = "已连接 %s 但没有外网（可能密码正确但该网络不通外网）" % ssid
    print("!! %s" % err)
    write_status(phase="failed", ssid=ssid, error=err)
    return err


# ------------------------------------------------------------ DNS 劫持
def dns_responder(portal_ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    bound = False
    for bind_ip in (portal_ip, "0.0.0.0"):
        try:
            s.bind((bind_ip, 53))
            bound = True
            break
        except Exception as e:
            print("!! DNS 绑定 %s:53 失败: %s（试下一个）" % (bind_ip, e))
    if not bound:
        print("!! DNS 劫持不可用，手机需手动打开 http://%s" % portal_ip)
        return
    print("DNS 劫持启动: 绑定 %s:53" % bind_ip)
    while RUNNING:
        try:
            data, addr = s.recvfrom(512)
            if len(data) < 12:
                continue
            tid = data[:2]
            # 解析问题数
            qd = struct.unpack("!H", data[4:6])[0]
            # 复制原查询的问题段
            i = 12
            for _ in range(qd):
                while i < len(data) and data[i] != 0:
                    i += data[i] + 1
                i += 5
            question = data[12:i]
            # 应答：标志位=0x8180(标准响应)，1 问题 1 答案，A 记录 TTL=10
            response = (tid + b"\x81\x80" + struct.pack("!HHHH", qd, 1, 0, 0)
                        + question
                        + struct.pack("!HHIH", 1, 1, 10, 4)
                        + socket.inet_aton(portal_ip))
            s.sendto(response, addr)
        except Exception:
            continue


# ------------------------------------------------------------ 配网页
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>台灯 WiFi 配网</title>
<style>
body{font-family:system-ui,'Microsoft YaHei',sans-serif;background:#f2f4f7;margin:0;padding:20px;color:#222}
h1{font-size:19px;margin:0 0 12px}
.card{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
select,input{width:100%;box-sizing:border-box;padding:10px;margin:6px 0;border:1px solid #ccc;border-radius:8px;font-size:15px}
button{width:100%;padding:12px;background:#2563eb;color:#fff;border:0;border-radius:8px;font-size:16px;margin-top:8px}
#msg{font-size:14px;color:#555;min-height:20px;margin-top:8px}
.ok{color:#16a34a;font-weight:600}.err{color:#dc2626}
</style></head><body>
<h1>💡 台灯 WiFi 配网</h1>
<div class="card">
<label>选择网络</label><select id="ssid"></select>
<label>或手动输入 WiFi 名称</label><input id="ssid_m" placeholder="留空则用上面选择的">
<label>WiFi 密码</label><input id="pwd" type="password" placeholder="开放网络可留空">
<button onclick="go()">连接</button>
<div id="msg"></div>
</div>
<script>
async function load(){
  const r=await fetch('/api/wifis');const j=await r.json();
  const sel=document.getElementById('ssid');sel.innerHTML='';
  (j.wifis||[]).forEach(w=>{
    const o=document.createElement('option');o.value=w.ssid;
    o.textContent=w.ssid+'  ('+w.signal+'% '+(w.sec||'')+')';sel.appendChild(o);
  });
  const st=(await (await fetch('/api/status')).json());
  const m=document.getElementById('msg');
  if(st.phase==='failed'){m.innerHTML='<span class="err">上次失败: '+st.error+'，请重试</span>';}
}
async function go(){
  const m=document.getElementById('msg');
  const sel=document.getElementById('ssid').value;
  const man=document.getElementById('ssid_m').value.trim();
  const ssid=man||sel; const pwd=document.getElementById('pwd').value;
  if(!ssid){m.textContent='请选择或输入 WiFi 名称';return;}
  m.textContent='正在连接 '+ssid+' ... 热点会短暂关闭，请稍候';
  await fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ssid:ssid,password:pwd})});
  const t0=Date.now();
  const t=setInterval(async()=>{
    const st=await (await fetch('/api/status')).json();
    if(st.phase==='connected'){clearInterval(t);m.innerHTML='<span class="ok">✅ 配网成功！'+st.ssid+' 已联网，热点即将关闭</span>';}
    else if(st.phase==='failed'){clearInterval(t);m.innerHTML='<span class="err">❌ '+st.error+'</span>';}
    else if(Date.now()-t0>40000){clearInterval(t);
      m.innerHTML='<span class="err">等待超时：若连上了但页面断了，说明大概率已成功——稍后直接用台灯即可</span>';}
  },1500);
}
load();setInterval(load,15000);
</script></body></html>"""


def http_server(portal_ip, do_connect):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            elif p == "/api/wifis":
                self._send(200, "application/json",
                           json.dumps({"wifis": scan_wifi()}).encode())
            elif p == "/api/status":
                self._send(200, "application/json",
                           json.dumps(read_status()).encode())
            else:
                # 任何未知路径（含 generate_204 探测）→ 跳配网页（触发手机弹窗）
                self.send_response(302)
                self.send_header("Location", "http://%s/" % portal_ip)
                self.end_headers()

        def do_POST(self):
            if self.path.split("?")[0] != "/api/connect":
                self._send(404, "application/json", b'{"ok":false}')
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n))
                ssid = str(req["ssid"]).strip()
                pwd = str(req.get("password", ""))
            except Exception:
                self._send(400, "application/json", b'{"ok":false}')
                return
            self._send(200, "application/json", b'{"ok":true}')
            threading.Thread(target=do_connect, args=(ssid, pwd),
                             daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", 80), Handler)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ------------------------------------------------------------ 主流程
def cmd_portal(args):
    write_status(phase="portal")
    print("=== 台灯配网门户启动 ===")
    portal_ip = "10.42.0.1"

    if not args.no_ap:
        portal_ip = start_hotspot(args.portal_ip)
        if portal_ip is None:
            write_status(phase="failed", error="无法开启热点")
            return 1
        print("请手机连接热点 %s（无密码），然后打开 http://%s" %
              (AP_SSID, portal_ip))
    else:
        portal_ip = wifi_ip()
        print("[--no-ap 调试模式] 不开热点，配网页: http://%s" % portal_ip)

    threading.Thread(target=dns_responder, args=(portal_ip,), daemon=True).start()

    result = {"done": False}

    def do_connect(ssid, pwd):
        err = try_connect(ssid, pwd)
        if err is None:
            result["done"] = True
            write_status(phase="connected", ssid=ssid)
        else:
            # 失败：重开热点让用户重试（IP 保持一致，页面 URL 不变）
            if not args.no_ap:
                start_hotspot(portal_ip)
            write_status(phase="failed", ssid=ssid, error=err)

    srv = http_server(portal_ip, do_connect)
    print("配网页运行中: http://%s (端口80)" % portal_ip)

    deadline = time.time() + args.timeout
    while RUNNING and not result["done"] and time.time() < deadline:
        time.sleep(0.5)

    print("门户退出（%s）" % ("配网成功" if result["done"] else
                           "超时/被停止"))
    if not result["done"]:
        write_status(phase="timeout")
    stop_hotspot()
    srv.shutdown()
    # 配网成功或超时后，确保 STA 恢复（NM 会自动重连已知网络）
    sh("nmcli device disconnect %s 2>/dev/null; nmcli device connect %s 2>/dev/null"
       % (WIFI_IF, WIFI_IF))
    return 0 if result["done"] else 1


def cmd_watchdog(args):
    """断网看门狗：连续 3 次无外网 → 拉起配网门户"""
    print("配网看门狗启动（每 %ds 检查一次，连续 %d 次断网自动开热点门户）"
          % (args.interval, args.miss))
    miss = 0
    while RUNNING:
        time.sleep(args.interval)
        if sh("systemctl is-active --quiet wifi-setup").returncode == 0:
            continue          # 门户已在跑
        if sh("systemctl is-active --quiet chatbot").returncode != 0:
            continue          # 主程序都没跑，别添乱
        if check_internet(3):
            miss = 0
        else:
            miss += 1
            print("断网检测 %d/%d" % (miss, args.miss))
            if miss >= args.miss:
                print("连续断网，自动启动配网门户")
                miss = 0
                sh("systemctl start wifi-setup", timeout=15)
    return 0


def cmd_connect(args):
    err = try_connect(args.ssid, args.password or "")
    print("结果: %s" % ("成功联网" if err is None else err))
    return 0 if err is None else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["portal", "watchdog", "connect"])
    ap.add_argument("--timeout", type=float, default=900,
                    help="门户最长运行秒数（超时自动恢复 STA）")
    ap.add_argument("--portal-ip", default=PORTAL_IP_DEFAULT,
                    help="门户/热点 IP（默认 192.168.0.1）")
    ap.add_argument("--no-ap", action="store_true",
                    help="调试：不开热点，只起配网页（不丢 SSH）")
    ap.add_argument("--ssid")
    ap.add_argument("--password")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--miss", type=int, default=3)
    args = ap.parse_args()
    os.makedirs(STATUS_DIR, exist_ok=True)
    if args.cmd == "portal":
        return cmd_portal(args)
    if args.cmd == "watchdog":
        return cmd_watchdog(args)
    return cmd_connect(args)


if __name__ == "__main__":
    sys.exit(main())

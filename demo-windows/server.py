#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lan-ip-manager 前端演示用的 Mock 后端（仅用于在 Windows 上展示界面与交互）。

为什么需要它
------------
真实项目的后端依赖 Linux 专有能力：读 /sys/class/net/<口>/carrier、调系统 ip /
netplan 命令、用 scapy 抓包、用 paramiko 走 SSH。这些在 Windows 上都跑不起来。
本文件用 Python **标准库** http.server 顶替后端，做到：
  - 原样提供前端页面（index.html + static/），界面与真实项目一致；
  - 把所有 /api/* 请求返回「预置的演示数据」，让三步流程点得动、看得见效果。
它不连接任何真实设备、不执行任何系统命令，纯演示用，零第三方依赖。

运行
----
    python server.py            # 默认 8000 端口，并自动打开浏览器
    python server.py 8080       # 指定端口
或直接双击「启动演示.bat」。

演示剧情（预置）
----------------
本机 enp3s0 有 192.168.1.20/24、WiFi wlan0 在 192.168.5.150/24；
直连发现一台 192.168.5.10 的设备（与本机不同网段，且和 WiFi 撞网段）；
测试连接回显它的网口是 eth0；改 IP 时演示「临时打通 → 备份 → 写入 → 应用 → 验证」。
"""

import importlib.util
import ipaddress
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8000

# --------------------------------------------------------------------------
# 复用真实项目的 netplan 脚本生成器（该模块只 import 标准库 ipaddress，可安全加载）。
# 加载不到时（例如只把 demo-windows 单独拷走），退回内置的等价实现。
# --------------------------------------------------------------------------
def _load_netplan():
    path = os.path.join(HERE, "..", "core", "netplan.py")
    try:
        spec = importlib.util.spec_from_file_location("demo_netplan", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


netplan = _load_netplan()


# ---- 内置兜底：仅当加载不到真实 netplan.py 时使用，输出与真实脚本结构一致 ----
def _fb_default_gateway_for(new_ip, prefix="24"):
    try:
        ip = ipaddress.ip_address((new_ip or "").strip())
        net = ipaddress.ip_network(f"{new_ip.strip()}/{str(prefix).strip()}", strict=False)
    except ValueError:
        return ""
    cand = net.network_address + 1
    if cand == ip:
        cand = net.network_address + 2
    if cand in net and cand != net.broadcast_address and cand != ip:
        return str(cand)
    return ""


def _fb_build_change_script(iface, new_ip, prefix="24", gateway=""):
    iface, new_ip, gateway = (iface or "").strip(), (new_ip or "").strip(), (gateway or "").strip()
    prefix = str(prefix or "24").strip()
    if not iface or not new_ip:
        raise ValueError("请先填写「对方网口名」和「新 IP」。")
    if not gateway:
        gateway = _fb_default_gateway_for(new_ip, prefix)
    return "\n".join([
        "# 这些值来自界面表单，先放进 shell 变量，后面拼配置时用。",
        f"IFACE='{iface}'", f"NEWIP='{new_ip}'", f"PREFIX='{prefix}'", f"GATEWAY='{gateway}'",
        "", "# 第①步 先删掉本工具上次可能写过的文件",
        "rm -f /etc/netplan/99-ipmanager.yaml",
        "", "# 第②步 读出对方原来的 renderer 和 nameservers，一会儿原样保留",
        "eval \"$(python3 /tmp/ipm_capture.py \"$IFACE\" 2>/dev/null)\"",
        "", "# 第③步 备份现有 netplan 配置到 /tmp/netplan-bak",
        "mkdir -p /tmp/netplan-bak", "cp /etc/netplan/*.yaml /tmp/netplan-bak/ 2>/dev/null || true",
        "", "# 第④步 禁用原有配置（改名成 .ipmbak，让 netplan 忽略）",
        "for f in /etc/netplan/*.yaml; do [ -e \"$f\" ] || continue; mv \"$f\" \"$f.ipmbak\"; done",
        "", "# 第⑤步 写入新配置",
        "} > /etc/netplan/99-ipmanager.yaml", "chmod 600 /etc/netplan/99-ipmanager.yaml",
        "", "# 第⑥步 后台应用",
        "nohup sh -c 'sleep 2; netplan apply' >/tmp/ipmanager_apply.log 2>&1 &",
        "echo '已提交：约 2 秒后新 IP 生效，本条 SSH 会随之断开，属正常现象。'",
    ])


def _fb_build_inspect_script():
    return "\n".join([
        "echo '===== 主机名 / 系统 ====='", "hostnamectl 2>/dev/null || hostname", "echo",
        "echo '===== 网口和 IP（ip -br addr）====='", "ip -br addr", "echo",
        "echo '===== 路由 / 网关（ip route）====='", "ip route", "echo",
        "echo '===== 当前 netplan 配置 ====='", "cat /etc/netplan/*.yaml 2>/dev/null", "echo",
        "echo '===== 网络配置系统探测 ====='",
        "ls /etc/netplan/*.yaml >/dev/null 2>&1 && echo '- netplan：有' || echo '- netplan：无'",
        "command -v nmcli >/dev/null 2>&1 && echo '- NetworkManager：有 nmcli' || true",
    ])


def _fb_build_show_backup_script():
    return "\n".join([
        "# 只读显示 /tmp/netplan-bak 里备份的对方原始 netplan 配置",
        "if [ -d /tmp/netplan-bak ] && ls /tmp/netplan-bak/*.yaml >/dev/null 2>&1; then",
        "  for f in /tmp/netplan-bak/*.yaml; do echo \"===== $f =====\"; cat \"$f\"; echo; done",
        "else echo '（/tmp/netplan-bak 下暂无备份文件）'; fi",
    ])


def build_change_script(iface, new_ip, prefix="24", gateway=""):
    if netplan is not None:
        return netplan.build_change_script(iface, new_ip, prefix, gateway)
    return _fb_build_change_script(iface, new_ip, prefix, gateway)


def build_inspect_script():
    return netplan.build_inspect_script() if netplan is not None else _fb_build_inspect_script()


def build_show_backup_script():
    return netplan.build_show_backup_script() if netplan is not None else _fb_build_show_backup_script()


def default_gateway_for(new_ip, prefix="24"):
    if netplan is not None:
        return netplan.default_gateway_for(new_ip, prefix)
    return _fb_default_gateway_for(new_ip, prefix)


# ==========================================================================
# 预置演示数据（形状与真实后端完全一致）
# ==========================================================================

# 本机网口：enp3s0 插了网线（有线直连目标），wlan0 在 192.168.5.x（和对方撞网段）
INTERFACES = [
    {
        "name": "enp3s0", "mac": "3c:52:82:1a:4b:07", "operstate": "up", "carrier": True,
        "addresses": ["192.168.1.20/24"],
        "ip_detail": [
            {"ip": "192.168.1.20", "prefix": 24, "cidr": "192.168.1.20/24", "kind": "permanent"},
        ],
    },
    {
        "name": "wlan0", "mac": "a4:c3:f0:9d:22:8e", "operstate": "up", "carrier": False,
        "addresses": ["192.168.5.150/24"],
        "ip_detail": [
            {"ip": "192.168.5.150", "prefix": 24, "cidr": "192.168.5.150/24", "kind": "dhcp"},
        ],
    },
    {
        "name": "enx00e04c6801a2", "mac": "00:e0:4c:68:01:a2", "operstate": "down", "carrier": False,
        "addresses": [], "ip_detail": [],
    },
]

# 扫描发现的设备（按真实规则排序：ARP 优先、非网关优先）
DEVICES = [
    {"mac": "5c:1e:3a:9f:00:10", "ip": "192.168.5.10", "via": "arp", "is_gateway": False, "seconds_ago": 0},
    {"mac": "28:d1:27:44:aa:01", "ip": "192.168.5.1", "via": "arp", "is_gateway": True, "seconds_ago": 1},
    {"mac": "b8:27:eb:12:34:56", "ip": "192.168.5.23", "via": "ip", "is_gateway": False, "seconds_ago": 3},
]

PEER_IP = "192.168.5.10"

INSPECT_STDOUT = """===== 主机名 / 系统 =====
Static hostname: edge-gateway-01
       Machine ID: 3f1c9d2a0b6e4c8f9a1b2c3d4e5f6071
         Boot ID: a1b2c3d4e5f60718293a4b5c6d7e8f90
  Operating System: Ubuntu 22.04.3 LTS

===== 网口和 IP（ip -br addr）=====
lo               UNKNOWN        127.0.0.1/8 ::1/128
eth0             UP             192.168.5.10/24 fe80::5e1e:3aff:fe9f:10/64

===== 路由 / 网关（ip route）=====
default via 192.168.5.1 dev eth0 proto static
192.168.5.0/24 dev eth0 proto kernel scope link src 192.168.5.10

===== 当前 netplan 配置 =====
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    eth0:
      dhcp4: false
      addresses: [192.168.5.10/24]
      routes:
        - to: default
          via: 192.168.5.1
      nameservers:
        addresses: [8.8.8.8, 1.1.1.1]

===== 网络配置系统探测 =====
- netplan：有
- NetworkManager：有 nmcli
"""

CHANGE_STDOUT = """将保留 renderer=NetworkManager nameservers=8.8.8.8,1.1.1.1
已写入 /etc/netplan/99-ipmanager.yaml：
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    eth0:
      dhcp4: false
      addresses: [192.168.4.200/24]
      routes:
        - to: default
          via: 192.168.4.1
      nameservers:
        addresses: [8.8.8.8,1.1.1.1]
已提交：约 2 秒后新 IP 生效，本条 SSH 会随之断开，属正常现象。
"""

BACKUP_STDOUT = """===== /tmp/netplan-bak/01-network-manager-all.yaml =====
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    eth0:
      dhcp4: false
      addresses: [192.168.5.10/24]
      routes:
        - to: default
          via: 192.168.5.1
      nameservers:
        addresses: [8.8.8.8, 1.1.1.1]
"""

# 访问日志窗口的滚动内容（演示用，每次 poll 吐一行）
_LOG_LINES = [
    '127.0.0.1 - - [{ts}] "GET /api/interfaces HTTP/1.1" 200 -',
    '127.0.0.1 - - [{ts}] "GET /api/scan_poll HTTP/1.1" 200 -',
    '127.0.0.1 - - [{ts}] "GET /api/logs?offset={off} HTTP/1.1" 200 -',
    '127.0.0.1 - - [{ts}] "POST /api/scan_start HTTP/1.1" 200 -',
    '127.0.0.1 - - [{ts}] "GET /api/scan_poll HTTP/1.1" 200 -',
]


# ==========================================================================
# 运行态（模拟「持续扫描」的渐进式发现）
# ==========================================================================
class DemoState:
    def __init__(self):
        self.lock = threading.Lock()
        self.scan_running = False
        self.scan_iface = None
        self.scan_polls = 0
        self.log_i = 0
        self.log_offset = 0

    def scan_start(self, iface):
        with self.lock:
            self.scan_running = True
            self.scan_iface = iface or "enp3s0"
            self.scan_polls = 0

    def scan_stop(self):
        with self.lock:
            self.scan_running = False

    def scan_snapshot(self):
        with self.lock:
            if self.scan_running:
                self.scan_polls += 1
            # 渐进式：第 1 次只露 1 台、第 2 次 2 台、之后全部，模拟真实「边扫边出现」
            n = len(DEVICES) if not self.scan_running else min(len(DEVICES), max(1, self.scan_polls))
            devices = [dict(d, seconds_ago=d["seconds_ago"]) for d in DEVICES[:n]]
            return {
                "ok": True,
                "devices": devices,
                "hidden_public": 2,
                "running": self.scan_running,
                "iface": self.scan_iface,
            }

    def next_log(self):
        with self.lock:
            self.log_i += 1
            self.log_offset += 1
            tmpl = _LOG_LINES[self.log_i % len(_LOG_LINES)]
            ts = time.strftime("%d/%b/%Y %H:%M:%S")
            text = tmpl.format(ts=ts, off=self.log_offset * 37)
            return {"ok": True, "text": text + "\n", "offset": self.log_offset}


STATE = DemoState()


# ==========================================================================
# HTTP 处理
# ==========================================================================
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "lan-ip-manager-demo"
    protocol_version = "HTTP/1.1"

    # 演示时把控制台请求日志静音
    def log_message(self, *args):
        pass

    # ---- 工具 ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, relpath):
        # 防目录穿越：规范化后必须仍在本目录内
        target = os.path.normpath(os.path.join(HERE, relpath))
        if not target.startswith(HERE):
            return self._json({"ok": False, "error": "forbidden"}, 403)
        if not os.path.isfile(target):
            return self._json({"ok": False, "error": "not found"}, 404)
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return self._serve_file("index.html")
        if path.startswith("/static/"):
            return self._serve_file(path.lstrip("/"))
        if path == "/api/interfaces":
            return self._json({"ok": True, "root": True, "interfaces": INTERFACES})
        if path == "/api/scan_poll":
            return self._json(STATE.scan_snapshot())
        if path == "/api/logs":
            return self._json(STATE.next_log())
        return self._json({"ok": False, "error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()

        if path == "/api/scan_start":
            STATE.scan_start((data.get("iface") or "").strip())
            return self._json({"ok": True})

        if path == "/api/scan_stop":
            STATE.scan_stop()
            return self._json({"ok": True})

        if path == "/api/quit":
            # 演示：返回 ok 让界面切到「已退出」提示；mock 进程不真的退出，刷新即可继续。
            return self._json({"ok": True})

        if path == "/api/local/add_ip":
            message = f"已在 {data.get('iface')} 上新增 IP {data.get('ip')}/{data.get('prefix', '24')}（演示，未真正改动系统）"
            return self._json({"ok": True, "message": message})

        if path == "/api/local/change_main":
            message = f"已把 {data.get('iface')} 的主 IP 替换为 {data.get('new_ip')}/{data.get('new_prefix', '24')}（演示，未真正改动系统）"
            return self._json({"ok": True, "message": message})

        if path == "/api/local/del_ip":
            message = f"已删除 {data.get('iface')} 上的 {data.get('ip')}/{data.get('prefix', '24')}（演示）"
            return self._json({"ok": True, "message": message})

        if path == "/api/ssh_test":
            if data.get("preview"):
                return self._json({"ok": True, "preview": True, "script": build_inspect_script()})
            temp = {
                "iface": data.get("iface") or "enp3s0",
                "local_ip": "192.168.5.254",
                "peer_ip": data.get("peer_ip") or PEER_IP,
            }
            return self._json({
                "ok": True, "rc": 0,
                "stdout": INSPECT_STDOUT, "stderr": "",
                "temp_used": temp,
            })

        if path == "/api/change_ip":
            iface = data.get("remote_iface")
            new_ip = data.get("new_ip")
            prefix = data.get("new_prefix", "24")
            try:
                script = build_change_script(iface, new_ip, prefix, data.get("gateway", ""))
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)})
            if data.get("preview"):
                return self._json({"ok": True, "preview": True, "script": script})
            gateway = (data.get("gateway") or "").strip() or default_gateway_for(new_ip, prefix)
            temp = {
                "iface": data.get("iface") or "enp3s0",
                "local_ip": "192.168.5.254",
                "peer_ip": data.get("peer_ip") or PEER_IP,
            }
            return self._json({
                "ok": True, "rc": 0,
                "stdout": CHANGE_STDOUT, "stderr": "",
                "script": script, "temp_used": temp,
                "new_ip": new_ip,
                "new_reachable": True,
                "verify_note": f"已连续 ping 通新 IP {new_ip}（ping -c 10，10 秒内收到响应）。默认网关 {gateway or '未配置'}。",
            })

        if path == "/api/show_backup":
            if data.get("preview"):
                return self._json({"ok": True, "preview": True, "script": build_show_backup_script()})
            temp = {
                "iface": data.get("iface") or "enp3s0",
                "local_ip": "192.168.5.254",
                "peer_ip": data.get("peer_ip") or PEER_IP,
            }
            return self._json({
                "ok": True, "rc": 0,
                "stdout": BACKUP_STDOUT, "stderr": "",
                "script": build_show_backup_script(), "temp_used": temp,
            })

        return self._json({"ok": False, "error": "not found"}, 404)


def _make_server(port):
    last = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            return httpd, p
        except OSError as e:
            last = e
    raise last


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    httpd, real_port = _make_server(port)
    url = f"http://127.0.0.1:{real_port}/"
    print("=" * 60)
    print(" lan-ip-manager 前端演示（Mock 后端，仅演示界面/交互）")
    print(f"   已启动：{url}")
    print("   提示：所有操作为预置数据演示，不会改动你的电脑；")
    print("         「退出程序」只是切换界面提示，刷新页面即可继续。")
    print("   停止：在本窗口按 Ctrl+C")
    print("=" * 60)
    if not os.environ.get("DEMO_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()

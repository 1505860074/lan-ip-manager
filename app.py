"""局域网设备 IP 管理面板 —— Flask 入口。

运行：
    sudo python3 app.py
然后浏览器打开 http://127.0.0.1:5000

为什么要 sudo：抓包（scapy）和给网卡加临时 IP（ip addr add）都需要 root。

面板工作流（三步，从左到右、从上到下）：
    ① 找到对方设备 选插网线的口并扫描，找出对方和它当前 IP
    ② 连接对方     填账号密码；测试能否登录（需要临时打通网络时自动完成、用完自动清理）
    ③ 修改对方 IP  填新 IP，预览命令后一键完成 备份->写入->应用->自动验证新 IP

关于「自动临时打通」
    连对方（测试连接 / 改 IP）时，如果本机和对方不在同一网段，本机根本发不出
    到对方的包。程序会自动给「本机直连网口」临时加一个和对方同网段的 IP，用完
    （SSH 结束、或验证完毕）立刻删掉，全程无需人工干预，也不污染本机原有配置。
"""
import getpass
import ipaddress
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

from flask import Flask, jsonify, render_template, request

from core import netplan, remote, scanner
from core.interfaces import list_interfaces
from core.sysutil import clean_env
from core.logsetup import read_access_log, setup_logging
from core.netconf import (
    add_alias,
    add_host_route,
    del_alias,
    del_host_route,
    pick_local_ip,
)
from core.sysutil import run_cmd


def resource_dir():
    """定位模板 / 静态文件所在目录。

    - 直接用 python 跑时：就是本文件所在目录。
    - 用 PyInstaller 打包成单文件后：运行时数据被解压到临时目录，
      路径存放在 sys._MEIPASS 里。这里做个兼容。
    """
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


_base = resource_dir()
app = Flask(
    __name__,
    template_folder=os.path.join(_base, "templates"),
    static_folder=os.path.join(_base, "static"),
)

# 配置本地日志（写到 logs/ 下的滚动文件）。放模块顶层而非仅 __main__，是为了
# 开发模式(FLASK_DEBUG=1)下重载器起的子进程导入本模块时也能配好日志。
setup_logging()
# 我们自己的应用日志：操作事件 / 错误异常都用它写；发现设备则在 discovery.py 里写。
log = logging.getLogger("ipmanager")

# ---- “关浏览器就自动关后端”用到的心跳状态 ----
# 页面开着时每隔约 1.5 秒会来一次轮询请求（见前端 pollLogs）。这里记下“最近一次收到
# 前端请求的时刻”；看门狗线程据此判断浏览器是不是已经关了（见 _start_idle_watchdog）。
_last_activity = None       # time.monotonic() 的值；None 表示还没收到过任何前端请求
IDLE_TIMEOUT = 10           # 连续这么多秒没有任何前端请求，就认为页面已关，自动退出


@app.before_request
def _mark_activity():
    """每收到一个请求就刷新“最近活动时间”。浏览器一关，请求就停，这个时间不再更新。"""
    global _last_activity
    _last_activity = time.monotonic()


def _is_root():
    # geteuid()==0 表示当前是 root。os.geteuid 只在 Linux/mac 有，符合本工具定位。
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _ssh_run(data, command, force_sudo=False):
    """通用 SSH 执行：建连 -> 跑一段命令 -> 关闭，返回 (rc, out, err)。

    用完即关、无状态。force_sudo=True 时一律以 root 执行（写 /etc/netplan、
    netplan apply 都需要）；sudo 密码留空则默认同登录密码（最常见）。
    连接 / 执行出错会抛异常，交给各路由自己 try 起来转成友好提示。
    """
    cli = remote.connect(
        data["peer_ip"],
        data["username"],
        data["password"],
        port=int(data.get("port", 22)),
    )
    try:
        use_sudo = force_sudo or bool(data.get("use_sudo"))
        sudo_pw = (data.get("sudo_password") or data["password"]) if use_sudo else None
        return remote.run_command(cli, command, sudo_pw=sudo_pw)
    finally:
        cli.close()


# ---------------------------------------------------------------------------
# 「自动临时打通」相关小工具
# 目标：连对方前，若本机与它不同网段，就临时加一个同网段 IP；用完自动删掉。
# ---------------------------------------------------------------------------
def _reachable_directly(target_ip):
    """本机是否已有一个和 target_ip 同网段的地址（不知道扫描口时的宽松兜底判断）。

    做法：遍历本机每个网口的每个 IPv4 地址（形如 "192.168.5.101/24"），
    把它当成一个网段，看 target_ip 是否落在里面。注意：同网段≠一定能连到，
    因为可能有别的网口（如 WiFi）撞了同一网段——所以有扫描口时优先用下面的
    _reachable_on_link_via 来精确判断。
    """
    try:
        target = ipaddress.ip_address(target_ip)
    except ValueError:
        return False
    for i in list_interfaces():
        for a in i["addresses"]:
            try:
                net = ipaddress.ip_network(a, strict=False)
            except ValueError:
                continue
            if target in net:
                return True
    return False


def _reachable_on_link_via(target_ip, iface):
    """系统当前去 target_ip 是不是【正好经由 iface 直连】（on-link，不经网关）。

    用 `ip route get` 看内核实际会怎么走：
      - 输出里有 "dev <iface>" 且没有 " via "（不经网关）→ 正好直连走这个口，True。
      - 走了别的口、或要经网关（有 " via "）→ False，需要我们把路由钉到 iface。
    这一步能识破「WiFi 撞了同网段、把包抢去无线」这种坑。
    """
    rc, out, _ = run_cmd(["ip", "route", "get", target_ip])
    if rc != 0:
        return False
    return (f"dev {iface}" in out) and (" via " not in out)


def _with_temp_reach(iface, target_ip, fn):
    """确保能【经由 iface 直连】到 target_ip，执行 fn()，最后无论成败都清理。

    返回 (fn 的返回值, temp_info)。temp_info 为 None 表示本来就直连、没动网络；
    否则是 {"iface","local_ip","peer_ip"}，说明临时加了源地址 + 主机路由（事后已删）。

    关键：直连设备可能和本机另一个网口（如 WiFi）用了同一网段，导致系统默认把
    包发去那个网口。所以这里不只看「有没有同网段地址」，而是确认「去 target 是不是
    正好走 iface」；不是的话，就在 iface 上加一个临时源地址(/32) + 一条 target/32
    主机路由，把去它的流量钉在 iface 上。用 /32 源地址是为了不新增 /24 连接路由、
    避免和 WiFi 的同网段路由打架。
    """
    ok = _reachable_on_link_via(target_ip, iface) if iface else _reachable_directly(target_ip)
    if ok:
        return fn(), None
    if not iface:
        raise RuntimeError(
            f"本机连不到 {target_ip}，且未确定本机直连网口——请先在 ① 扫描一次。"
        )
    src_ip = pick_local_ip(target_ip, 24)
    add_alias(iface, src_ip, 32)                # 临时源地址；/32 不产生 /24 路由，避免和 WiFi 冲突
    add_host_route(iface, target_ip, src_ip)    # /32 主机路由，把这台设备的流量钉在 iface
    temp = {"iface": iface, "local_ip": src_ip, "peer_ip": target_ip}
    try:
        return fn(), temp
    finally:
        try:
            del_host_route(iface, target_ip)
        except Exception:
            pass
        try:
            del_alias(iface, src_ip, 32)
        except Exception:
            pass


def _verify_new_ip(iface, new_ip):
    """改完后自动验证：本机能不能 ping 通对方的新 IP。返回 (是否通 or None, 说明文本)。

    对方改 IP 后可能又落到一个本机够不着 / 会走错口的网段，所以复用 _with_temp_reach
    把路由钉到 iface 再 ping。对方 netplan apply 要几秒才生效，故用 `ping -c 10 -i 1`
    连发 10 个包、跨约 10 秒，只要有一个回应就算通。
    """
    if not new_ip:
        return None, ""

    def do_ping():
        rc, _, _ = run_cmd(["ping", "-c", "10", "-i", "1", "-W", "1", new_ip], timeout=20)
        return rc

    try:
        rc, _temp = _with_temp_reach(iface, new_ip, do_ping)
    except Exception as e:
        return None, f"无法自动验证新 IP：{e}"
    if rc == 0:
        return True, f"已 ping 通 {new_ip}，新 IP 生效。"
    return False, (
        f"约 10 秒内 ping 不通 {new_ip}：对方可能还没起来（可稍等再测），"
        f"或新 IP / 网关填错。若对方还连得上，可点「显示备份文件」看回原配置、手动改回；"
        f"若彻底失联，只能物理接触对方恢复。"
    )


@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# ① 找到对方设备：列本机网口 + 在选定口上扫描发现设备
# ---------------------------------------------------------------------------
@app.route("/api/interfaces")
def api_interfaces():
    """返回本机所有网口 + 是否插网线，并告知前端当前是不是 root。"""
    try:
        return jsonify(ok=True, root=_is_root(), interfaces=list_interfaces())
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/api/scan_start", methods=["POST"])
def api_scan_start():
    """开始在指定网口上【持续】被动抓包（后台线程）。前端随后每秒来 poll 取结果。"""
    if not _is_root():
        return jsonify(ok=False, error="需要以 root 运行才能抓包扫描，请用 sudo 启动本程序。")
    data = request.get_json(force=True)
    iface = data["iface"]
    my_mac = next(
        (i["mac"] for i in list_interfaces() if i["name"] == iface), None
    )
    try:
        scanner.session.start(iface, my_mac=my_mac)
        log.info("开始扫描 iface=%s", iface)
        return jsonify(ok=True)
    except Exception as e:
        log.error("开始扫描失败 iface=%s: %s", iface, e)
        return jsonify(ok=False, error=str(e))


@app.route("/api/logs")
def api_logs():
    """把 logs/access.log 的新增内容吐给前端「访问日志」窗口，实现 tail -f 效果。

    前端传 offset=<上次读到的字节位置>，只取增量（首次传 0 取文件末尾一小段）。
    """
    offset = request.args.get("offset", default=0, type=int)
    text, new_offset = read_access_log(offset)
    return jsonify(ok=True, text=text, offset=new_offset)


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """面板上「退出程序」按钮调用它：关闭整个后端进程。

    为什么用后台线程 + os._exit
    -------------------------
    直接在请求里退出，浏览器那边的请求会没头没脑地断开、看到报错。所以先正常把
    ok 返回给前端（让它显示“已退出”），再由一个后台线程延时半秒把进程干掉。
    os._exit(0) 是“立即退出”，不走清理流程——这正是我们要的：抓包线程、Flask 都随之
    结束。若是双击提权启动的，本进程（root）退出后，外层等待它的父进程也会跟着退出。
    """
    log.info("用户在面板点击「退出程序」，后端即将关闭")

    def killer():
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=killer, daemon=True).start()
    return jsonify(ok=True)


@app.route("/api/scan_poll")
def api_scan_poll():
    """取当前累积到的设备快照（前端每秒调一次，实现边扫边更新）。"""
    r = scanner.session.poll()
    return jsonify(
        ok=True,
        devices=r["devices"],
        hidden_public=r["hidden_public"],
        running=r["running"],
        iface=r["iface"],
    )


@app.route("/api/scan_stop", methods=["POST"])
def api_scan_stop():
    """停止后台抓包（结果保留，最后一次 poll 仍能取到）。"""
    scanner.session.stop()
    log.info("停止扫描")
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# ② 连接对方：测试登录（需要时自动临时打通网络、用完自动清理）
# ---------------------------------------------------------------------------
@app.route("/api/ssh_test", methods=["POST"])
def api_ssh_test():
    """用填好的账号密码登录对方，回显只读诊断信息（主机名/网口/netplan 等）。

    既验证「能不能登录」，又方便你抄下对方的网口名，填到第③步。
    若本机和对方不同网段，会自动临时加同网段 IP 连过去，读完立刻删掉。
    """
    data = request.get_json(force=True)
    # 预览：只返回「测试连接」时将在对方机器上执行的只读命令文本，
    # 不连对方、不读账号密码——和第③步「预览」完全一致，保证所见即所跑。
    if data.get("preview"):
        return jsonify(ok=True, preview=True, script=netplan.build_inspect_script())
    try:
        (rc, out, err), temp = _with_temp_reach(
            data.get("iface"),
            data["peer_ip"],
            lambda: _ssh_run(data, netplan.build_inspect_script(), force_sudo=True),
        )
        log.info(
            "测试连接 peer=%s user=%s rc=%s temp_used=%s",
            data.get("peer_ip"), data.get("username"), rc, bool(temp),
        )
        return jsonify(ok=True, rc=rc, stdout=out, stderr=err, temp_used=temp)
    except Exception as e:
        log.error("测试连接失败 peer=%s: %s", data.get("peer_ip"), e)
        return jsonify(ok=False, error=f"SSH 连接或执行失败: {e}")


# ---------------------------------------------------------------------------
# ③ 修改对方 IP：预览 / 执行 / 显示备份（都是同一段命令，保证「所见即所跑」）
# ---------------------------------------------------------------------------
@app.route("/api/change_ip", methods=["POST"])
def api_change_ip():
    """改对方 IP。preview=true 只返回将执行的命令文本，不连对方；
    否则用完全相同的这段命令通过 SSH 执行 备份->写入->应用。
    """
    data = request.get_json(force=True)
    try:
        script = netplan.build_change_script(
            data.get("remote_iface"),
            data.get("new_ip"),
            data.get("new_prefix", "24"),
            data.get("gateway", ""),
        )
    except ValueError as e:
        return jsonify(ok=False, error=str(e))

    if data.get("preview"):
        return jsonify(ok=True, preview=True, script=script)

    # 全自动执行：①（需要时）临时打通到对方当前 IP → ② SSH 跑 备份/写入/应用
    #            → ③ 清理刚才的临时 IP → ④ 自动验证对方新 IP 是否 ping 得通。
    iface = data.get("iface")
    # 记录实际生效的网关：用户填了用填的，没填则记自动推出的那个（和脚本里一致）
    eff_gw = (data.get("gateway") or "").strip() or netplan.default_gateway_for(
        data.get("new_ip"), data.get("new_prefix", "24"))
    log.info(
        "改 IP 开始 peer=%s remote_iface=%s new_ip=%s/%s gateway=%s",
        data.get("peer_ip"), data.get("remote_iface"),
        data.get("new_ip"), data.get("new_prefix", "24"), eff_gw or "(无)",
    )
    try:
        (rc, out, err), temp = _with_temp_reach(
            iface,
            data["peer_ip"],
            lambda: _ssh_run(data, script, force_sudo=True),
        )
    except Exception as e:
        log.error("改 IP 执行失败 peer=%s: %s", data.get("peer_ip"), e)
        return jsonify(ok=False, error=f"SSH 连接或执行失败: {e}")

    # 验证新 IP（同样按需把路由钉到 iface、验证完清理）
    new_ip = (data.get("new_ip") or "").strip()
    new_reachable, verify_note = _verify_new_ip(iface, new_ip)
    log.info(
        "改 IP 完成 peer=%s rc=%s new_ip=%s new_reachable=%s",
        data.get("peer_ip"), rc, new_ip, new_reachable,
    )

    return jsonify(
        ok=True,
        rc=rc,
        stdout=out,
        stderr=err,
        script=script,
        temp_used=temp,               # 连对方时是否临时加过 IP（已自动删除）
        new_ip=new_ip,
        new_reachable=new_reachable,  # True=通 / False=不通 / None=没法自动验证
        verify_note=verify_note,
    )


@app.route("/api/show_backup", methods=["POST"])
def api_show_backup():
    """显示对方 /tmp/netplan-bak/ 里备份的原始 netplan 配置（只读，不改动对方）。

    连到「对方当前 IP」框里填的地址、按需自动临时打通、用完清理；把读到的备份
    原始内容原样返回给前端显示，同时**完整写进本机日志** logs/app.log。
    """
    data = request.get_json(force=True)
    script = netplan.build_show_backup_script()
    if data.get("preview"):
        return jsonify(ok=True, preview=True, script=script)
    try:
        (rc, out, err), temp = _with_temp_reach(
            data.get("iface"),
            data["peer_ip"],
            lambda: _ssh_run(data, script, force_sudo=True),
        )
        # 把备份文件的原始内容完整写入日志（多行，故换行后原样写入，方便事后追溯）
        log.info(
            "显示备份文件 peer=%s rc=%s，备份原始内容如下：\n%s",
            data.get("peer_ip"), rc, out or "（无内容）",
        )
        return jsonify(ok=True, rc=rc, stdout=out, stderr=err, script=script, temp_used=temp)
    except Exception as e:
        log.error("显示备份文件失败 peer=%s: %s", data.get("peer_ip"), e)
        return jsonify(ok=False, error=f"SSH 连接或执行失败: {e}")


def _has_terminal():
    """判断当前是不是在一个真正的终端窗口里运行。

    原理：终端会给进程接上一个 tty（终端设备）作为标准输入/输出；`isatty()` 就是问
    “你连着的是不是终端设备”。从终端里 `./程序` 或 `sudo ./程序` 启动时，stdin/stdout
    都连着终端，isatty() 为真；而在文件管理器里【双击】启动时没有终端，isatty() 为假。
    只要 stdin 或 stdout 有一个是终端，就认为有终端。
    """
    try:
        return sys.stdin.isatty() or sys.stdout.isatty()
    except Exception:
        # 某些环境下这几个流可能是 None（比如被打包/重定向），取不到就当没有终端
        return False


def _gui_notify(message):
    """没有终端时，尽量用图形方式弹个提示（终端里 print 用户根本看不到）。

    依次尝试常见的图形提示工具，谁在就用谁；全都没有也不报错（尽力而为）：
      - zenity     GNOME 常见的弹窗工具
      - xmessage   最老牌、几乎必装的 X 弹窗
      - notify-send 桌面右上角的通知气泡
    """
    for cmd in (
        ["zenity", "--error", "--no-wrap", "--text", message],
        ["xmessage", "-center", message],
        ["notify-send", "局域网 IP 管理面板", message],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=clean_env())
                return
            except Exception:
                continue


def _open_browser_later(url):
    """服务器起来后，自动用浏览器打开面板页。尽力而为，失败也绝不影响服务本身。

    为什么放到后台线程 + 延时
    ----------------------
    app.run() 是阻塞的（它一直跑着处理请求，不会往下返回），所以不能在它之后再开
    浏览器。这里先起一个后台线程，睡一会儿等 Flask 把端口真正监听起来，再去开浏览器。

    为什么要处理 sudo
    ----------------
    本工具通常用 `sudo` 跑（需要 root 抓包/改网卡），进程身份是 root；但浏览器和桌面
    会话属于当初登录的那个普通用户。用 root 直接开浏览器经常失败（Chrome 拒绝以 root
    运行、或连不上用户的图形界面）。所以若检测到是 sudo 提权来的（环境变量 SUDO_USER
    有值），就用 `sudo -u <原用户>` 切回原用户身份去开浏览器，并把图形会话需要的
    DISPLAY / XAUTHORITY 一并带过去。
    """
    def worker():
        time.sleep(1.5)  # 给 Flask 一点时间把端口监听起来，太早打开会连不上
        try:
            sudo_user = os.environ.get("SUDO_USER")
            if sudo_user and sudo_user != "root" and shutil.which("xdg-open"):
                # 组装要传给原用户的图形会话环境变量（缺了浏览器就找不到桌面）
                env_args = []
                if os.environ.get("DISPLAY"):
                    env_args.append(f"DISPLAY={os.environ['DISPLAY']}")
                if os.environ.get("XAUTHORITY"):
                    env_args.append(f"XAUTHORITY={os.environ['XAUTHORITY']}")
                cmd = ["sudo", "-u", sudo_user, "env"] + env_args + ["xdg-open", url]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env=clean_env())
            else:
                # 没用 sudo（或本来就是普通用户跑）：直接用标准库开默认浏览器
                webbrowser.open(url)
        except Exception:
            pass  # 打不开就算了：终端里有醒目提示，用户照着手动打开即可

    threading.Thread(target=worker, daemon=True).start()


def _start_idle_watchdog():
    """后台看门狗：浏览器窗口一关，前端轮询就停；连续 IDLE_TIMEOUT 秒没收到任何请求，
    就自动关掉后端进程。这样用户直接关浏览器标签，也能顺带把程序关干净，不必非点“退出”。

    只有在收到过至少一次请求后（_last_activity 不为 None）才开始计时，避免程序刚起、
    浏览器还没连上就被误杀；刷新页面只会断一两秒，远小于 IDLE_TIMEOUT，不会误杀。
    """
    def worker():
        while True:
            time.sleep(2)
            if _last_activity is None:
                continue
            if time.monotonic() - _last_activity > IDLE_TIMEOUT:
                log.info("检测到浏览器已关闭（%d 秒无请求），后端自动退出", IDLE_TIMEOUT)
                os._exit(0)

    threading.Thread(target=worker, daemon=True).start()


def _print_banner(url, is_root, debug):
    """在终端打印一个醒目的方框提示，告诉用户「面板地址」和「怎么用」。

    单独打印方框，是因为 Flask 自己启动时会刷一堆日志/警告，容易把普通一行提示盖住；
    方框更显眼，用户一眼能找到该打开的网址。
    """
    line = "=" * 60
    print("\n" + line)
    print("  局域网 IP 管理面板 已启动")
    print(f"  请在浏览器打开： {url}")
    print("  （已尝试自动打开浏览器；若没弹出，请手动复制上面的网址访问）")
    print("  停止：在本窗口按 Ctrl+C")
    if not is_root:
        print("  【警告】当前不是 root，抓包和改网卡会失败，请改用 sudo 重新启动。")
    if debug:
        print("  【开发模式】改完 .py 会自动重启（改 HTML/JS/CSS 直接刷新浏览器即可）。")
    print(line + "\n")


# ---------------------------------------------------------------------------
# 提权：让工具始终以 root 身份运行（抓包、改网卡都要 root）。
# 不再要求用户“终端里 sudo 启动”，而是：普通身份启动 → 要一个 sudo 密码 →
# 用它把本程序自己以 root 重新拉起。这样双击也能全功能用。
# ---------------------------------------------------------------------------
def _ask_sudo_password(retry=False):
    """要一个本机 sudo 密码。返回密码字符串；用户取消/拿不到则返回 None。

    有终端就在终端里问（getpass，输入不回显）；没有终端（双击启动）就弹图形密码框。
    retry=True 表示上一次密码错了，提示语里带上“请重试”。
    """
    tip = "输入本机 sudo 密码" + ("（密码错误，请重试）" if retry else "")
    if _has_terminal():
        try:
            return getpass.getpass(f"[{tip}]: ")
        except (EOFError, KeyboardInterrupt):
            return None
    # 没有终端：依次尝试图形密码框，谁在用谁
    for cmd in (
        ["zenity", "--password", "--title", tip],
        ["kdialog", "--password", tip],
    ):
        if shutil.which(cmd[0]):
            try:
                p = subprocess.run(cmd, capture_output=True, env=clean_env())
                if p.returncode == 0:            # 用户输入并确定
                    return p.stdout.decode(errors="replace").rstrip("\n")
                return None                      # 用户点了取消
            except Exception:
                continue
    # 既没终端又没有图形密码框：无法要到密码，只能提示改用终端
    _gui_notify(
        "无法弹出密码输入框。\n请打开终端，用以下命令启动本程序：\n    sudo "
        + os.path.abspath(sys.argv[0])
    )
    return None


def _sudo_passwordless():
    """探测当前用户的 sudo 是不是「免密码」的。

    `sudo -n -v`：-n 表示“绝不弹密码提示”，-v 表示“验证能否提权”。若配了免密
    （NOPASSWD），它直接成功返回 0；若需要密码，因为不让弹提示，会失败返回非 0。
    据此判断要不要弹密码框。
    """
    try:
        p = subprocess.run(["sudo", "-n", "-v"], capture_output=True, timeout=10,
                            env=clean_env())
        return p.returncode == 0
    except Exception:
        return False


def _sudo_password_ok(password):
    """校验 sudo 密码对不对：用 `sudo -S -k -v` 走一遍。

    -S 从标准输入读密码；-k 先清掉可能已缓存的授权，保证这次真的按密码验证；
    -v 只做“验证身份”不执行别的命令。返回码 0 即密码正确。
    """
    try:
        p = subprocess.run(
            ["sudo", "-S", "-k", "-v"],
            input=(password + "\n").encode(),
            capture_output=True,
            timeout=15,
            env=clean_env(),
        )
        return p.returncode == 0
    except Exception:
        return False


def _relaunch_as_root(password):
    """用 sudo 把“本程序自己”以 root 身份重新启动，然后让当前（普通身份）进程退出。

    - 打包成单文件后 sys.executable 就是那个可执行文件本身；开发时则是 python 解释器，
      要把脚本路径 sys.argv 一起带上。用 sys.frozen 区分这两种情况。
    - --preserve-env=DISPLAY,XAUTHORITY：让 root 子进程仍能拿到图形会话信息，
      这样它待会儿才能（切回普通用户身份）自动打开浏览器。
    - communicate() 会把密码喂给 sudo 并【阻塞等待】root 子进程结束：用户在面板点“退出”
      让 root 进程退出后，这里的父进程也随之退出，整棵进程树干净收场。
    """
    if getattr(sys, "frozen", False):
        prog = [sys.executable] + sys.argv[1:]
    else:
        prog = [sys.executable] + sys.argv
    # sudo 默认清空环境变量。除了图形会话要用的 DISPLAY/XAUTHORITY，还要把本程序认得的
    # HOST/PORT/FLASK_DEBUG 以及 PYTHONNOUSERSITE 一并保留，否则 root 子进程会丢掉这些设置
    # （比如 PORT 丢了就退回默认 5000）。
    keep = "DISPLAY,XAUTHORITY,HOST,PORT,FLASK_DEBUG,PYTHONNOUSERSITE"
    cmd = ["sudo", "-S", "-p", "", "--preserve-env=" + keep] + prog
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=clean_env())
        proc.communicate((password + "\n").encode())
        sys.exit(proc.returncode)
    except SystemExit:
        raise
    except Exception as e:
        _gui_notify(f"以 root 身份重启失败：{e}")
        sys.exit(1)


def _ensure_root_or_exit():
    """确保以 root 运行：已经是 root 就直接返回；不是就要密码、校验、以 root 重启。

    最多让用户试 3 次密码；取消或连错 3 次就退出（不把没权限的半吊子进程留着）。
    重启成功时 _relaunch_as_root 内部会退出本进程，不会返回到这里。
    """
    if _is_root():
        return
    # sudo 免密码时，弹框问一个用不上的密码很多余：直接以 root 重启。
    if _sudo_passwordless():
        _relaunch_as_root("")      # 免密，密码传空串即可；此函数会在内部退出本进程
    for attempt in range(3):
        pw = _ask_sudo_password(retry=(attempt > 0))
        if pw is None:                 # 用户取消 / 拿不到密码
            _gui_notify("已取消，未启动。")
            sys.exit(0)
        if _sudo_password_ok(pw):
            _relaunch_as_root(pw)      # 成功：以 root 重启，本进程在此退出
    _gui_notify("sudo 密码错误次数过多，已退出。")
    sys.exit(1)


if __name__ == "__main__":
    # ---- 确保以 root 运行 ----
    # 双击或普通身份启动时，这里会要一个 sudo 密码，用它把本程序以 root 重新拉起；
    # 已经是 root（比如从终端 sudo 启动）则直接往下走。这样双击也能全功能使用。
    # 重启成功时本进程会在 _ensure_root_or_exit 内部退出，不会执行到下面。
    # 注意：debug 的重载子进程（WERKZEUG_RUN_MAIN=true）继承父进程身份，无需再提权。
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        _ensure_root_or_exit()

    # 这几项都可用环境变量覆盖，方便开发脚本控制（都有合理默认值，不设也能跑）：
    #   HOST / PORT   监听地址和端口
    #   FLASK_DEBUG=1 打开调试模式：改完 .py 自动重启 + 出错时浏览器显示详细堆栈
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    # 默认关闭 debug：打包成单文件后，Werkzeug 的自动重载器会和 scapy / 打包环境冲突。
    # 开发时想要“改完自动重启”，用 FLASK_DEBUG=1 打开即可（见 dev.sh）。
    debug = os.environ.get("FLASK_DEBUG", "") == "1"

    # 浏览器里要访问的地址：监听在 0.0.0.0 时本机仍用 127.0.0.1 打开。
    open_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{open_host}:{port}"

    _print_banner(url, _is_root(), debug)

    # debug=True 时 Werkzeug 会用重载器起两个进程（父进程只是监工），自动开浏览器要放到
    # 真正干活的子进程里，否则会开两次或开早了。用 WERKZEUG_RUN_MAIN 区分：它只在
    # 子进程里为 "true"。非 debug 模式没有这个变量，直接开即可。
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _open_browser_later(url)
        _start_idle_watchdog()   # 关浏览器 → 一段时间无请求 → 自动关后端

    app.run(host=host, port=port, debug=debug)

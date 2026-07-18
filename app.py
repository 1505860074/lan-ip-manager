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
import ipaddress
import os
import sys

from flask import Flask, jsonify, render_template, request

from core import netplan, remote, scanner
from core.interfaces import list_interfaces
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
        f"约 10 秒内 ping 不通 {new_ip}：对方可能还没起来，"
        f"或新 IP / 网关填错。必要时可点「还原到备份」回退。"
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
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


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
        return jsonify(ok=True, rc=rc, stdout=out, stderr=err, temp_used=temp)
    except Exception as e:
        return jsonify(ok=False, error=f"SSH 连接或执行失败: {e}")


# ---------------------------------------------------------------------------
# ③ 修改对方 IP：预览 / 执行 / 还原（都是同一段命令，保证「所见即所跑」）
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
    try:
        (rc, out, err), temp = _with_temp_reach(
            iface,
            data["peer_ip"],
            lambda: _ssh_run(data, script, force_sudo=True),
        )
    except Exception as e:
        return jsonify(ok=False, error=f"SSH 连接或执行失败: {e}")

    # 验证新 IP（同样按需把路由钉到 iface、验证完清理）
    new_ip = (data.get("new_ip") or "").strip()
    new_reachable, verify_note = _verify_new_ip(iface, new_ip)

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


@app.route("/api/restore_ip", methods=["POST"])
def api_restore_ip():
    """还原到备份。preview=true 只返回命令文本、不连对方。

    执行时连到「对方当前 IP」框里填的地址（改成功后对方在新 IP 上，就填新 IP）；
    同样按需自动临时打通、用完清理。
    """
    data = request.get_json(force=True)
    script = netplan.build_restore_script()
    if data.get("preview"):
        return jsonify(ok=True, preview=True, script=script)
    try:
        (rc, out, err), temp = _with_temp_reach(
            data.get("iface"),
            data["peer_ip"],
            lambda: _ssh_run(data, script, force_sudo=True),
        )
        return jsonify(ok=True, rc=rc, stdout=out, stderr=err, script=script, temp_used=temp)
    except Exception as e:
        return jsonify(ok=False, error=f"SSH 连接或执行失败: {e}")


if __name__ == "__main__":
    # 这几项都可用环境变量覆盖，方便开发脚本控制（都有合理默认值，不设也能跑）：
    #   HOST / PORT   监听地址和端口
    #   FLASK_DEBUG=1 打开调试模式：改完 .py 自动重启 + 出错时浏览器显示详细堆栈
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    # 默认关闭 debug：打包成单文件后，Werkzeug 的自动重载器会和 scapy / 打包环境冲突。
    # 开发时想要“改完自动重启”，用 FLASK_DEBUG=1 打开即可（见 dev.sh）。
    debug = os.environ.get("FLASK_DEBUG", "") == "1"

    print(f"面板已启动，请用浏览器打开： http://{host}:{port}")
    if not _is_root():
        print("【警告】当前不是 root，抓包和改网卡会失败，请用 sudo 重新启动。")
    if debug:
        print("【开发模式】已开启自动重启：改完 .py 会自动生效（改 HTML/JS/CSS 直接刷新浏览器即可）。")
    app.run(host=host, port=port, debug=debug)

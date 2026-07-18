"""在本机网口上临时增加/删除一个 IP，用来「把网段调整到和对方一致」。

为什么要这一步？
----------------
发现对方 IP（比如 192.168.5.10/24）后，如果本机网口上没有同网段的地址，
仍然连不上对方。这里就给本机网口临时挂一个同网段地址（比如 192.168.5.254），
让两边处于同一网段，之后才能 ping / SSH 到对方。

这些地址是「临时」的：只用 `ip addr add` 加在内存里，不写任何配置文件，
本机重启后自动消失，不会污染本机原有网络配置。
"""
import ipaddress

from .sysutil import run_cmd


def pick_local_ip(peer_ip, prefix):
    """在对方所在网段里，挑一个不与对方冲突的地址给本机临时用。

    例：对方 192.168.5.10/24 -> 挑 192.168.5.254 给本机。
    做法：取网段里「广播地址 - 1」这个最大可用地址；若正好等于对方 IP，
    就再往前退一个。
    """
    net = ipaddress.ip_network(f"{peer_ip}/{prefix}", strict=False)
    last = int(net.broadcast_address) - 1
    cand = ipaddress.ip_address(last)
    if str(cand) == peer_ip:
        cand = ipaddress.ip_address(last - 1)
    return str(cand)


def add_alias(iface, ip, prefix):
    """给网口加一个临时 IP（别名地址）。"""
    rc, out, err = run_cmd(["ip", "addr", "add", f"{ip}/{prefix}", "dev", iface])
    # 如果这个地址已经存在，ip 会报 "File exists"，这属于「已经加过了」，当成功处理。
    if rc != 0 and "File exists" not in err:
        raise RuntimeError(f"添加临时 IP 失败: {err}")
    return f"{ip}/{prefix}"


def del_alias(iface, ip, prefix):
    """移除之前加的临时 IP。"""
    rc, out, err = run_cmd(["ip", "addr", "del", f"{ip}/{prefix}", "dev", iface])
    # "Cannot assign" 一般是本来就没有这个地址，忽略即可。
    if rc != 0 and "Cannot assign" not in err:
        raise RuntimeError(f"删除临时 IP 失败: {err}")


def add_host_route(iface, peer_ip, src_ip):
    """加一条只针对 peer_ip 的 /32 主机路由，把去它的流量「钉」在 iface 上。

    为什么需要它？
    --------------
    当本机另一个网口（比如 WiFi）和对方用了同一网段（如都用 192.168.5.x），
    系统会按那个网口的 /24 路由把包发错方向。这里加一条 peer_ip/32 的路由：
    /32 是「精确到这一台」的路由，比任何 /24 都更具体，会被优先匹配，从而
    强制去 peer_ip 的流量走 iface（你真正插网线的那个口）。
    src 指定用哪个源地址发出去（须是 iface 上一个和对方同网段的地址）。
    """
    rc, out, err = run_cmd(
        ["ip", "route", "add", f"{peer_ip}/32", "dev", iface, "src", src_ip]
    )
    # 已存在同样的路由时会报 "File exists"，视为「已经钉好了」，当成功。
    if rc != 0 and "File exists" not in err:
        raise RuntimeError(f"添加主机路由失败: {err}")


def del_host_route(iface, peer_ip):
    """移除之前加的 /32 主机路由。"""
    rc, out, err = run_cmd(["ip", "route", "del", f"{peer_ip}/32", "dev", iface])
    # 本来就没有这条路由时会报错，忽略即可。
    if rc != 0 and "No such process" not in err and "not found" not in err.lower():
        raise RuntimeError(f"删除主机路由失败: {err}")

"""枚举本机网口，并判断每个网口是否插了网线（carrier）。"""
import json

from .localnet import netplan_static_addrs
from .sysutil import run_cmd


def _read_sys(iface, name):
    """读取 /sys/class/net/<网口>/<属性> 这个内核暴露的文件。

    Linux 把每个网口的状态放在 /sys 下面，用普通文件读写就能拿到，
    比如 carrier 文件内容为 "1" 表示网线已插且对端有信号。
    网口 down 的时候读 carrier 会报错，这里统一返回 None。
    """
    path = f"/sys/class/net/{iface}/{name}"
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def list_interfaces():
    """返回本机所有网口的信息列表。

    每个元素是一个字典：
      name       网口名，如 eth0 / enp3s0
      mac        MAC 地址（网卡硬件地址）
      operstate  网口状态：up / down / unknown ...
      carrier    True = 网线已插入并连通
      addresses  这个网口当前的 IPv4 地址列表，如 ["192.168.1.5/24"]
      ip_detail  每个 IPv4 地址的明细，含来源标注，形如：
                 [{"ip": "192.168.1.5", "prefix": 24,
                   "cidr": "192.168.1.5/24", "kind": "permanent"}]
                 kind 取值：
                   permanent 写在 netplan 里，重启保留，可从 netplan 删；
                   dhcp      内核标了 dynamic，是 DHCP 动态获取的租约；
                   manual    只在运行态、既不在 netplan 也不是 DHCP，
                             多半是 `ip addr` 临时加的（重启失效）。
    """
    # `ip -j addr` 的 -j 参数让 ip 直接输出 JSON，省得我们自己解析文本表格。
    rc, out, err = run_cmd(["ip", "-j", "addr"])
    if rc != 0:
        raise RuntimeError(f"执行 `ip -j addr` 失败: {err}")
    data = json.loads(out)

    # 先把 netplan 里静态写死的地址、以及开了 DHCP 的网口读出来，用于给每个地址标来源。
    static_addrs, dhcp_ifaces = netplan_static_addrs()

    result = []
    for item in data:
        name = item.get("ifname")
        if name == "lo":
            # lo 是回环网口（127.0.0.1），不是真实物理口，跳过。
            continue
        # addr_info 里既有 IPv4(inet) 也有 IPv6(inet6)，这里只挑 IPv4。
        addrs = []
        ip_detail = []
        for a in item.get("addr_info", []):
            if a.get("family") != "inet":
                continue
            ip = a["local"]
            prefix = a["prefixlen"]
            cidr = f"{ip}/{prefix}"
            addrs.append(cidr)
            # 判断来源，优先级：
            #  1) 在 netplan 静态地址集合里 -> permanent（重启保留、可从 netplan 删）
            #  2) 内核标了 dynamic，或该口在 netplan 开了 dhcp4 -> dhcp（DHCP 动态租约）
            #  3) 其余 -> manual（运行态手动地址，多半是 ip addr 临时加的，重启失效）
            if cidr in static_addrs.get(name, set()):
                kind = "permanent"
            elif a.get("dynamic") or name in dhcp_ifaces:
                kind = "dhcp"
            else:
                kind = "manual"
            ip_detail.append(
                {"ip": ip, "prefix": prefix, "cidr": cidr, "kind": kind}
            )
        carrier = _read_sys(name, "carrier")
        result.append(
            {
                "name": name,
                "mac": item.get("address"),
                "operstate": item.get("operstate"),
                "carrier": carrier == "1",
                "addresses": addrs,
                "ip_detail": ip_detail,
            }
        )
    return result

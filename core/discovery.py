"""发现直连到本机的设备（被动抓包 + 智能过滤）。

抓包原理
--------
当对方设备用网线直连、但它的 IP 与本机不在同一网段时，本机没法用 IP
去主动 ping 它（三层不通）。但只要网线是通的，两台机器就处在同一个
「二层（链路层）」网络里，对方主动发出的广播 / 组播包（ARP 通告、
mDNS、DHCP 请求等）本机都能收到。

所以这里在指定网口上「被动抓包」，从抓到的包里提取对方的 MAC 和 IP ——
这样即便网段不同，也能发现它、并知道它当前用的网段。

本模块拆成两块，供「持续扫描」复用：
  - update_seen(seen, pkt)  处理【一个】抓到的包，把证据累加进 seen；
  - classify(seen)          把累积的 seen 转成排序/过滤后的设备列表。
持续扫描见 core/scanner.py：它用后台线程反复调用 update_seen，前端每秒
调 classify 取快照。

过滤规则（让结果更贴近「真正直连的设备」）：
  1. 优先 ARP：ARP 里的 (IP, MAC) 是设备「自己声明」的，一一对应、最可信；
  2. 过滤公网源 IP：直连设备不会用公网地址，这类多半是网关转发的「路过流量」，默认隐藏；
  3. 标记疑似网关：同一个 MAC 关联很多个不同源 IP 的，基本是路由器在转发，打标签。
"""
import ipaddress
import logging
import time

from scapy.all import ARP, IP, Ether

# 一个 MAC 关联的不同「源 IP」数量达到这个阈值，就判定为疑似网关（在转发流量）。
GATEWAY_IP_THRESHOLD = 3

# 应用日志（配置在 core/logsetup.py，app.py 启动时挂好 handler）。
# 抓包在高频回调里跑，所以只在“某个 MAC 首次出现”时记一条，避免每个包都写爆日志。
log = logging.getLogger("ipmanager")


def _is_public(ip):
    """判断一个 IPv4 是不是公网地址。

    Python 的 ipaddress 把 RFC1918 私网（10/8、172.16/12、192.168/16）、
    127 回环、169.254 链路本地等都归为 is_private=True；除此之外
    （比如 68.79.31.154）就是公网。解析失败时保守地当成「非公网」（返回 False）。
    """
    try:
        return not ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def update_seen(seen, pkt, my_mac=None):
    """处理【一个】抓到的包，把设备证据就地累加进 seen（不返回新对象）。

    seen 结构：{mac: {"arp_ip": str|None, "ip_ips": set(), "last_seen": float}}
      arp_ip    —— 若该 MAC 通过 ARP 露过面，这里存它自己声明的 IP（最可信）
      ip_ips    —— 从普通 IP 包里看到的、以该 MAC 为源的所有源 IP（可能很杂）
      last_seen —— 最后一次听到它的时刻（time.monotonic），用于「消失」老化
    my_mac：本机该网口 MAC，用来过滤掉「自己发出去」的包。
    单个坏包不该中断整个抓包，所以整体包在 try 里。
    """
    try:
        if my_mac and pkt.haslayer(Ether) and pkt[Ether].src.lower() == my_mac.lower():
            return  # 本机自己发的，忽略

        now = time.monotonic()
        if pkt.haslayer(ARP):
            mac = pkt[ARP].hwsrc
            ip = pkt[ARP].psrc
            if not mac or not ip or ip == "0.0.0.0":
                return
            is_new = mac not in seen  # setdefault 前先判断，才知道是不是第一次见到
            rec = seen.setdefault(mac, {"arp_ip": None, "ip_ips": set(), "last_seen": now})
            rec["arp_ip"] = ip  # ARP 的 psrc 是设备自己声明的真实 IP，直接采用
            rec["last_seen"] = now
            if is_new:
                log.info("扫描发现设备 mac=%s ip=%s via=arp", mac, ip)
        elif pkt.haslayer(IP) and pkt.haslayer(Ether):
            mac = pkt[Ether].src
            ip = pkt[IP].src
            if not mac or not ip or ip == "0.0.0.0":
                return
            is_new = mac not in seen
            rec = seen.setdefault(mac, {"arp_ip": None, "ip_ips": set(), "last_seen": now})
            rec["ip_ips"].add(ip)
            rec["last_seen"] = now
            if is_new:
                log.info("扫描发现设备 mac=%s ip=%s via=ip", mac, ip)
    except Exception:
        pass


def classify(seen, now=None):
    """把累积的 seen 转成排序 / 过滤后的结果 dict：

      {"devices": [{"mac","ip","via","is_gateway","seconds_ago"}, ...], "hidden_public": int}

    via: "arp" = ARP 发现（可信，排最前）；"ip" = 仅从普通 IP 包凑出来（仅供参考）。
    seconds_ago: 距离最后一次听到它过了多少秒（传了 now 才有，否则为 None）。
    """
    devices = []
    hidden_public = 0
    for mac, rec in seen.items():
        # 同一个 MAC 关联了很多不同源 IP => 疑似网关在转发流量
        is_gateway = len(rec["ip_ips"]) >= GATEWAY_IP_THRESHOLD
        seconds_ago = None
        if now is not None and "last_seen" in rec:
            seconds_ago = max(0, int(now - rec["last_seen"]))

        if rec["arp_ip"]:
            devices.append(
                {"mac": mac, "ip": rec["arp_ip"], "via": "arp",
                 "is_gateway": is_gateway, "seconds_ago": seconds_ago}
            )
            continue

        # 只有普通 IP 包的证据：只保留私网源 IP 来代表它；公网的算「路过流量」隐藏
        private_ips = [ip for ip in rec["ip_ips"] if not _is_public(ip)]
        if not private_ips:
            hidden_public += len(rec["ip_ips"])
            continue
        devices.append(
            {"mac": mac, "ip": sorted(private_ips)[0], "via": "ip",
             "is_gateway": is_gateway, "seconds_ago": seconds_ago}
        )

    # 排序：ARP 优先；同类里「非网关」优先；再按 IP 排，稳定好看。
    def sort_key(d):
        return (
            0 if d["via"] == "arp" else 1,
            1 if d["is_gateway"] else 0,
            d["ip"],
        )

    devices.sort(key=sort_key)
    return {"devices": devices, "hidden_public": hidden_public}

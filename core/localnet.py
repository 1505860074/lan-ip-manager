"""配置「本机自己」网口的 IP：临时(ip addr)或永久(写 netplan)。

和 netconf.py 的区别
--------------------
- netconf.py 里的临时别名，是为了「连对方」临时打通网络，用完立刻删、不落盘。
- 这里是用户主动要给本机网口配 IP：既能临时加(重启失效)，也能永久写进 netplan
  (重启保留)，还能改网口的主 IP、删掉之前加的地址。

两种「生效时长」的实现方式完全不同
--------------------------------
- 临时：直接用 `ip addr add/del`，只改内核里的运行态，不碰任何配置文件，重启就没了。
- 永久：改 /etc/netplan/*.yaml 里的静态配置，再 `netplan apply` 让它立刻生效，
  重启后依然保留。

关于 netplan 的一个坑：多份 *.yaml 会被「合并」使用
------------------------------------------------
netplan 会把 /etc/netplan/ 下所有 *.yaml 合起来一起用。所以「改主 IP(替换)」时，
必须把该网口在**所有** yaml 里的旧静态地址都清掉，否则旧地址会和新地址合并、残留下来。
本模块的 change_main_ip 就是这么做的。

本模块所有函数都假定进程已是 root(改本机网络必须 root)，由 app.py 的路由层保证。
"""
import glob
import ipaddress
import os
import shutil

import yaml

from .sysutil import run_cmd

NETPLAN_DIR = "/etc/netplan"
# 当某个网口在现有 netplan 里「压根没被配置过」时(比如它本来靠 DHCP)，
# 我们把新配置写进这个本工具专属文件，不去动别人的文件。
MANAGED_FILE = os.path.join(NETPLAN_DIR, "90-ipmanager-local.yaml")
# 改 netplan 前，把原文件静默备份到这里(界面上不做「一键回滚」，但留个备份总没坏处)。
LOCAL_BACKUP_DIR = "/tmp/netplan-bak-local"


# ---------------------------------------------------------------------------
# 参数校验：IP / 前缀 / 网关 / DNS 都先验一遍，非法就抛 ValueError（上层转友好提示）
# ---------------------------------------------------------------------------
def validate_ip_prefix(ip, prefix):
    """校验 IP 和前缀合法，返回规范化后的 (ip_str, prefix_int)。"""
    ip = (ip or "").strip()
    prefix = str(prefix or "").strip()
    if not ip:
        raise ValueError("请填写 IP 地址。")
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError(f"IP 地址不合法：{ip}")
    try:
        p = int(prefix)
    except ValueError:
        raise ValueError(f"子网前缀必须是数字：{prefix}")
    if not (0 <= p <= 32):
        raise ValueError(f"子网前缀必须在 0~32 之间：{prefix}")
    return ip, p


def _validate_gateway(gateway):
    gateway = (gateway or "").strip()
    if not gateway:
        return ""
    try:
        ipaddress.ip_address(gateway)
    except ValueError:
        raise ValueError(f"网关地址不合法：{gateway}")
    return gateway


def _validate_dns(dns):
    """dns 可以是「用逗号/空格分隔的字符串」或列表，返回规范化后的地址列表。"""
    if not dns:
        return []
    if isinstance(dns, str):
        parts = dns.replace(",", " ").split()
    else:
        parts = list(dns)
    out = []
    for d in parts:
        d = str(d).strip()
        if not d:
            continue
        try:
            ipaddress.ip_address(d)
        except ValueError:
            raise ValueError(f"DNS 地址不合法：{d}")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 临时操作（ip addr）：只改运行态，重启失效
# ---------------------------------------------------------------------------
def add_ip_temp(iface, ip, prefix):
    """给网口临时加一个 IP（别名地址）。已存在则视为成功。"""
    rc, out, err = run_cmd(["ip", "addr", "add", f"{ip}/{prefix}", "dev", iface])
    if rc != 0 and "File exists" not in err:
        raise RuntimeError(f"临时新增 IP 失败：{err.strip() or out.strip()}")
    return f"已临时新增 {ip}/{prefix} 到 {iface}（重启后失效）。"


def del_ip_temp(iface, ip, prefix):
    """临时删除网口上的一个 IP。本来就没有则视为成功。"""
    rc, out, err = run_cmd(["ip", "addr", "del", f"{ip}/{prefix}", "dev", iface])
    if rc != 0 and "Cannot assign" not in err:
        raise RuntimeError(f"临时删除 IP 失败：{err.strip() or out.strip()}")
    return f"已临时删除 {iface} 上的 {ip}/{prefix}。"


def change_main_ip_temp(iface, old_ip, old_prefix, new_ip, new_prefix):
    """临时「改主 IP」：先删掉旧主 IP，再加上新的（都只改运行态）。

    old_ip 可以留空——那样就只加新 IP、不删旧的（等于新增）。
    """
    if old_ip:
        # 删旧地址失败(比如它已经不在)不致命，继续加新的即可。
        run_cmd(["ip", "addr", "del", f"{old_ip}/{old_prefix}", "dev", iface])
    rc, out, err = run_cmd(["ip", "addr", "add", f"{new_ip}/{new_prefix}", "dev", iface])
    if rc != 0 and "File exists" not in err:
        raise RuntimeError(f"临时改主 IP 失败：{err.strip() or out.strip()}")
    return f"已临时把 {iface} 主 IP 改为 {new_ip}/{new_prefix}（重启后失效）。"


# ---------------------------------------------------------------------------
# netplan 读写：永久操作的底层
# ---------------------------------------------------------------------------
def _list_yaml_files():
    return sorted(glob.glob(os.path.join(NETPLAN_DIR, "*.yaml")))


def _load(path):
    """读一个 netplan yaml，返回 dict；读不了或空文件返回空骨架。"""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return {}
    return data


def _load_all():
    """返回 [(path, data-dict), ...]，只含能解析成功的文件。"""
    result = []
    for path in _list_yaml_files():
        data = _load(path)
        if data is not None:
            result.append((path, data))
    return result


def _ethernets(data):
    """安全取 data['network']['ethernets']（是个 dict）；缺了返回空 dict。"""
    net = data.get("network") or {}
    eth = net.get("ethernets") or {}
    return eth if isinstance(eth, dict) else {}


def _addr_strings(addresses):
    """把 netplan 的 addresses 字段规范成 ["ip/prefix", ...] 字符串列表。

    netplan 里 addresses 常见是字符串列表：["192.168.1.5/24"]；
    偶尔某项是带选项的字典 {"192.168.1.5/24": {...}}，这里取它的键。
    """
    out = []
    for a in addresses or []:
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict) and a:
            out.append(str(next(iter(a.keys()))))
    return out


def detect_global_renderer():
    """从现有 netplan 文件里找出全局 renderer（networkd / NetworkManager）。

    新建本工具专属文件时带上它，避免把网口的「网络管家」悄悄换掉。
    """
    for _, data in _load_all():
        net = data.get("network") or {}
        r = net.get("renderer")
        if r:
            return str(r)
    return ""


def netplan_static_addrs():
    """扫描所有 netplan 文件，返回：
      addrs: {iface: set("ip/prefix", ...)}  —— 静态写死的地址
      dhcp:  set(iface, ...)                  —— 开了 dhcp4 的网口

    interfaces.py 用它来判断某个运行中的地址是「永久(在 netplan 里)」还是
    「临时/动态」。
    """
    addrs = {}
    dhcp = set()
    for _, data in _load_all():
        for iface, conf in _ethernets(data).items():
            if not isinstance(conf, dict):
                continue
            for s in _addr_strings(conf.get("addresses")):
                addrs.setdefault(iface, set()).add(s)
            if conf.get("dhcp4") in (True, "true", "yes"):
                dhcp.add(iface)
    return addrs, dhcp


def _backup_all():
    """把当前所有 netplan *.yaml 静默备份到 LOCAL_BACKUP_DIR。"""
    os.makedirs(LOCAL_BACKUP_DIR, exist_ok=True)
    for path in _list_yaml_files():
        try:
            shutil.copy2(path, os.path.join(LOCAL_BACKUP_DIR, os.path.basename(path)))
        except OSError:
            pass


def _write(path, data):
    """把 dict 写回 netplan yaml，权限 600（netplan 要求配置文件不可被他人读）。"""
    text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False,
                          allow_unicode=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _apply():
    """netplan apply 让配置立刻生效。本机服务绑在 127.0.0.1，改口不影响本页面。"""
    rc, out, err = run_cmd(["netplan", "apply"], timeout=60)
    if rc != 0:
        raise RuntimeError(f"netplan apply 失败：{err.strip() or out.strip()}")


def _iface_conf_locations(iface):
    """返回所有「定义了该网口」的位置：[(path, data, eth_conf_dict), ...]。"""
    locs = []
    for path, data in _load_all():
        eth = _ethernets(data)
        if iface in eth and isinstance(eth[iface], dict):
            locs.append((path, data, eth[iface]))
    return locs


def _ensure_managed_skeleton():
    """读出（或新建）本工具专属文件的骨架 dict。"""
    data = _load(MANAGED_FILE) if os.path.exists(MANAGED_FILE) else None
    if not data:
        data = {"network": {"version": 2}}
    net = data.setdefault("network", {})
    net.setdefault("version", 2)
    renderer = detect_global_renderer()
    if renderer and "renderer" not in net:
        net["renderer"] = renderer
    net.setdefault("ethernets", {})
    return data


# ---------------------------------------------------------------------------
# 永久操作（改 netplan）
# ---------------------------------------------------------------------------
def add_ip_permanent(iface, ip, prefix):
    """永久新增一个别名 IP：往该网口的 addresses 里追加一条，保留其它配置不动。

    若该网口在现有 netplan 里没被配置过，则写进本工具专属文件 MANAGED_FILE。
    """
    _backup_all()
    target = f"{ip}/{prefix}"
    locs = _iface_conf_locations(iface)
    if locs:
        path, data, conf = locs[0]
        cur = _addr_strings(conf.get("addresses"))
        if target not in cur:
            cur.append(target)
        conf["addresses"] = cur
        _write(path, data)
    else:
        data = _ensure_managed_skeleton()
        conf = data["network"]["ethernets"].setdefault(iface, {})
        cur = _addr_strings(conf.get("addresses"))
        if target not in cur:
            cur.append(target)
        conf["addresses"] = cur
        _write(MANAGED_FILE, data)
    _apply()
    return f"已永久新增 {target} 到 {iface}（已写入 netplan，重启保留）。"


def del_ip_permanent(iface, ip, prefix):
    """永久删除一个地址：从该网口在**所有** netplan 文件里的 addresses 中移除它。"""
    _backup_all()
    target = f"{ip}/{prefix}"
    removed = False
    for path, data, conf in _iface_conf_locations(iface):
        cur = _addr_strings(conf.get("addresses"))
        if target in cur:
            cur = [a for a in cur if a != target]
            conf["addresses"] = cur
            _write(path, data)
            removed = True
    if not removed:
        raise RuntimeError(f"在 netplan 里没找到 {iface} 上的 {target}，无法删除。")
    _apply()
    return f"已从 netplan 删除 {iface} 上的 {target}（重启保留）。"


def change_main_ip_permanent(iface, new_ip, new_prefix, gateway="", dns=None):
    """永久改主 IP：把该网口在所有 netplan 文件里的旧静态地址清空，
    再在主文件里写上新的 IP，可一并配置默认网关和 DNS。

    为什么要清所有文件：netplan 会合并多份 yaml，只改一处的话别处的旧 IP 会残留。

    gateway 可留空；dns 可以是字符串("8.8.8.8, 1.1.1.1")或列表，内部会自行校验规范化。
    """
    _backup_all()
    gateway = _validate_gateway(gateway)
    dns = _validate_dns(dns)   # 统一成合法地址列表（传字符串也能正确切分，不会被拆成单字符）
    target = f"{new_ip}/{new_prefix}"
    locs = _iface_conf_locations(iface)

    if locs:
        main_path, main_data, main_conf = locs[0]
        # 先把「非主文件」里这个网口的旧地址清掉，避免合并残留。
        for path, data, conf in locs[1:]:
            if "addresses" in conf:
                conf.pop("addresses", None)
                _write(path, data)
    else:
        # 网口没被配置过：用本工具专属文件当主文件。
        main_data = _ensure_managed_skeleton()
        main_path = MANAGED_FILE
        main_conf = main_data["network"]["ethernets"].setdefault(iface, {})

    main_conf["dhcp4"] = False
    main_conf["addresses"] = [target]
    if gateway:
        main_conf["routes"] = [{"to": "default", "via": gateway}]
    else:
        # 没填网关就把旧的默认路由去掉，免得改到新网段后留着一条通不了的旧网关。
        main_conf.pop("routes", None)
        main_conf.pop("gateway4", None)  # 老写法，一并清掉
    if dns:
        main_conf["nameservers"] = {"addresses": list(dns)}
    _write(main_path, main_data)
    _apply()

    extra = []
    if gateway:
        extra.append(f"网关 {gateway}")
    if dns:
        extra.append("DNS " + ",".join(dns))
    tail = ("；" + "、".join(extra)) if extra else ""
    return f"已永久把 {iface} 主 IP 改为 {target}{tail}（已写入 netplan，重启保留）。"

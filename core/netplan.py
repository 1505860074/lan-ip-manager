"""集中生成「在对方机器上执行」的 netplan 相关命令文本。

为什么要有这个模块
------------------
界面上「预览将执行的命令」和「真正执行」必须是**同一段命令**——
否则预览给你看一套、实际却跑另一套，就很危险。所以这里用同一个函数
生成命令文本：预览时把它显示给你看，执行时把**完全相同**的这段文本
通过 SSH 发到对方去跑。你在界面上看到的，就是实际会执行的。

这些命令都是明文 shell，没有任何隐藏逻辑：备份、写配置、应用、查看备份，
每一步都能在预览里看清楚。目前只覆盖 netplan（Ubuntu 18.04+ 默认）。
"""
import ipaddress

# 备份目录：改配置前，把对方原有的 netplan yaml 都拷一份到这里，方便随时查看原配置、
# 万一改错时照着手工恢复（本工具已不再提供自动还原，改用「显示备份文件」只读查看）。
BACKUP_DIR = "/tmp/netplan-bak"
# 本工具写入的配置文件名。注意：netplan 会把 /etc/netplan/ 下所有 *.yaml **合并**
# 起来一起用，光靠“编号大”并不能真正覆盖旧文件里同一网口的地址（旧地址会残留、
# 变成“叠加”而非“替换”）。所以改 IP 时我们会把原有 *.yaml 临时改名成 *.ipmbak
# 让 netplan 忽略它们，只留下本文件生效，从而实现真正的替换。改名后的 *.ipmbak
# 会一直留在对方 /etc/netplan/ 下（不自动改回）；需要恢复时照「显示备份文件」里的
# 内容手工处理即可。
DROPIN = "/etc/netplan/99-ipmanager.yaml"
# 被临时“禁用”的原配置文件的后缀：netplan 只读 .yaml，改成这个后缀就相当于关掉。
DISABLED_SUFFIX = ".ipmbak"


def default_gateway_for(new_ip, prefix="24"):
    """从 IP/前缀推出该网段“约定俗成”的默认网关：网络号 + 1（如 192.168.4.0/24 -> .1）。

    推不出合理值（IP 非法、或前缀太小如 /31/32 放不下一个网关）时返回 ""。
    若 +1 正好等于设备自己的 IP（比如设备就设成了 .1），则退而取 +2，避免“自己当自己网关”。
    """
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


def build_change_script(iface, new_ip, prefix="24", gateway=""):
    """生成「备份 -> 写入新配置 -> 后台应用」的完整脚本文本。

    参数都来自界面表单；iface / new_ip 必填，缺了会抛 ValueError，
    由上层转成「请先填写…」的友好提示。

    默认网关是**强制**配置的（这样对方会持续 ARP 找网关、才能被扫描发现）：
      - 用户填了 gateway 就用用户的；
      - 用户留空则自动取该网段的默认网关（default_gateway_for，网络号 + 1）。
    """
    iface = (iface or "").strip()
    new_ip = (new_ip or "").strip()
    prefix = str(prefix or "24").strip()
    gateway = (gateway or "").strip()
    if not iface or not new_ip:
        raise ValueError("请先填写「对方网口名」和「新 IP」。")

    # 用户没填网关就按网段自动取一个；netplan v2 用 routes 配默认网关（老的 gateway4 已废弃）。
    if not gateway:
        gateway = default_gateway_for(new_ip, prefix)
    routes = ""
    if gateway:
        routes = (
            "\n      routes:"
            "\n        - to: default"
            f"\n          via: {gateway}"
        )

    yaml = (
        "network:\n"
        "  version: 2\n"
        "  ethernets:\n"
        f"    {iface}:\n"
        "      dhcp4: false\n"
        f"      addresses: [{new_ip}/{prefix}]"
        + routes
    )

    return "\n".join([
        f"# 第①步 先删掉本工具上次可能写过的文件（避免它被当成“原配置”备份/禁用）",
        f"rm -f {DROPIN}",
        "",
        f"# 第②步 备份现有 netplan 配置到 {BACKUP_DIR}（此时只剩对方真正的原配置）",
        f"mkdir -p {BACKUP_DIR}",
        f"cp /etc/netplan/*.yaml {BACKUP_DIR}/ 2>/dev/null || true",
        "",
        "# 第③步 禁用原有配置：把每个 *.yaml 改名成 *.ipmbak，让 netplan 忽略它们。",
        "#        这是“真替换”的关键——否则旧文件里的旧 IP 会和新 IP 合并、一起残留。",
        "for f in /etc/netplan/*.yaml; do",
        '  [ -e "$f" ] || continue          # 没有任何 .yaml 时，通配符不展开，跳过',
        f'  mv "$f" "$f{DISABLED_SUFFIX}"',
        "done",
        "",
        f"# 第④步 写入新配置到 {DROPIN}（现在它是唯一生效的 netplan 文件）",
        f"cat > {DROPIN} <<'EOF'",
        yaml,
        "EOF",
        f"chmod 600 {DROPIN}",
        f"echo '已写入 {DROPIN}：'; cat {DROPIN}",
        "",
        "# 第⑤步 后台应用（sleep 2 让这条 SSH 先干净返回；IP 一变当前连接就会断，属正常）",
        "nohup sh -c 'sleep 2; netplan apply' >/tmp/ipmanager_apply.log 2>&1 &",
        "echo '已提交：约 2 秒后新 IP 生效，本条 SSH 会随之断开，属正常现象。'",
    ])


def build_show_backup_script():
    """生成一段**只读**脚本：把 {BACKUP_DIR} 里备份的对方原始 netplan 配置逐个打印出来。

    只 cat 文件内容，不改动对方任何东西。用于「显示备份文件」——让你随时能看到
    改 IP 之前对方长什么样（前端会把这份原始内容原样显示，并完整写进本机日志）。
    """
    return "\n".join([
        f"# 只读显示 {BACKUP_DIR} 里备份的对方原始 netplan 配置（不改动对方任何东西）",
        f"if [ -d {BACKUP_DIR} ] && ls {BACKUP_DIR}/*.yaml >/dev/null 2>&1; then",
        f"  for f in {BACKUP_DIR}/*.yaml; do",
        '    echo "===== $f ====="',
        '    cat "$f"',
        "    echo",
        "  done",
        "else",
        f"  echo '（{BACKUP_DIR} 下暂无备份文件：可能还没执行过「修改对方 IP」，或备份已被清理。）'",
        "fi",
    ])


def build_inspect_script():
    """生成一段**只读**诊断脚本：看对方主机名、网口/IP、路由、netplan 配置、
    以及它用的是哪套网络配置系统。用于「测试连接」——既确认能登录，
    又方便你抄下对方的网口名填到第④步。全程不改动对方任何配置。
    """
    return "\n".join([
        "echo '===== 主机名 / 系统 ====='",
        "hostnamectl 2>/dev/null || hostname",
        "echo",
        "echo '===== 网口和 IP（ip -br addr）====='",
        "ip -br addr",
        "echo",
        "echo '===== 路由 / 网关（ip route）====='",
        "ip route",
        "echo",
        "echo '===== 当前 netplan 配置 ====='",
        "cat /etc/netplan/*.yaml 2>/dev/null || echo '（没有 /etc/netplan/*.yaml）'",
        "echo",
        "echo '===== 网络配置系统探测 ====='",
        "ls /etc/netplan/*.yaml >/dev/null 2>&1 && echo '- netplan：有' || echo '- netplan：无'",
        "command -v nmcli >/dev/null 2>&1 && echo '- NetworkManager：有 nmcli' || true",
        "test -f /etc/network/interfaces && echo '- ifupdown：有 /etc/network/interfaces' || true",
        "ls /etc/systemd/network/*.network >/dev/null 2>&1 && echo '- systemd-networkd：有' || true",
    ])

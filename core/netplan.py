"""集中生成「在对方机器上执行」的 netplan 相关命令文本。

为什么要有这个模块
------------------
界面上「预览将执行的命令」和「真正执行」必须是**同一段命令**——
否则预览给你看一套、实际却跑另一套，就很危险。所以这里用同一个函数
生成命令文本：预览时把它显示给你看，执行时把**完全相同**的这段文本
通过 SSH 发到对方去跑。你在界面上看到的，就是实际会执行的。

这些命令都是明文 shell，没有任何隐藏逻辑：备份、写配置、应用、还原，
每一步都能在预览里看清楚。目前只覆盖 netplan（Ubuntu 18.04+ 默认）。
"""

# 备份目录：改配置前，把对方原有的 netplan yaml 都拷一份到这里，方便还原。
BACKUP_DIR = "/tmp/netplan-bak"
# 本工具写入的配置文件名：数字大 => netplan 里优先级高，能覆盖同名网口的旧设置。
DROPIN = "/etc/netplan/99-ipmanager.yaml"


def build_change_script(iface, new_ip, prefix="24", gateway=""):
    """生成「备份 -> 写入新配置 -> 后台应用」的完整脚本文本。

    参数都来自界面表单；iface / new_ip 必填，缺了会抛 ValueError，
    由上层转成「请先填写…」的友好提示。
    """
    iface = (iface or "").strip()
    new_ip = (new_ip or "").strip()
    prefix = str(prefix or "24").strip()
    gateway = (gateway or "").strip()
    if not iface or not new_ip:
        raise ValueError("请先填写「对方网口名」和「新 IP」。")

    # 填了网关才多写一段 routes；netplan v2 用 routes 配默认网关（老的 gateway4 已废弃）。
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
        f"# 第①步 备份现有 netplan 配置到 {BACKUP_DIR}",
        f"mkdir -p {BACKUP_DIR}",
        f"cp /etc/netplan/*.yaml {BACKUP_DIR}/ 2>/dev/null || true",
        "",
        f"# 第②步 写入新配置到独立文件 {DROPIN}",
        f"cat > {DROPIN} <<'EOF'",
        yaml,
        "EOF",
        f"chmod 600 {DROPIN}",
        f"echo '已写入 {DROPIN}：'; cat {DROPIN}",
        "",
        "# 第③步 后台应用（sleep 2 让这条 SSH 先干净返回；IP 一变当前连接就会断，属正常）",
        "nohup sh -c 'sleep 2; netplan apply' >/tmp/ipmanager_apply.log 2>&1 &",
        "echo '已提交：约 2 秒后新 IP 生效，本条 SSH 会随之断开，属正常现象。'",
    ])


def build_restore_script():
    """生成「把备份拷回去 -> 删掉本工具写的文件 -> 后台重新应用」的脚本文本。"""
    return "\n".join([
        f"# 把 {BACKUP_DIR} 里备份的 yaml 拷回 /etc/netplan/，并删掉本工具写的那个文件",
        f"cp {BACKUP_DIR}/*.yaml /etc/netplan/ 2>/dev/null || true",
        f"rm -f {DROPIN}",
        "# 后台重新应用，让还原后的配置生效",
        "nohup sh -c 'sleep 2; netplan apply' >/tmp/ipmanager_apply.log 2>&1 &",
        "echo '已还原备份并在后台重新应用；若当前是通过被改的网口连的，SSH 可能断开。'",
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

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


# 一段在对方机器上跑的 python3 代码：解析对方原来的 netplan 配置，把「由谁管网络」
# （renderer）和「DNS 服务器」（nameservers）读出来，打印成 shell 可 eval 的赋值。
# 为什么要保留这两样
# ----------------
# 之前只写一份最简静态配置、还把原配置全禁用，等于悄悄把网络管家从 NetworkManager
# 换成了 systemd-networkd（renderer 没写就默认 networkd），也丢掉了原来的 DNS。
# NetworkManager 会周期性做联网检查、为查 DNS 反复 ARP 找网关——设备正是靠这个被扫描
# 发现的。换成安静的 networkd 后设备就“哑”了、扫不到了。所以改 IP 时必须原样保留它们。
#
# 读取来源按优先级找：当前 *.yaml → 被本工具禁用的 *.ipmbak → /tmp 备份。这样无论是
# 第一次改（原配置还在 *.yaml），还是对已改过的设备再改（原值只剩在 .ipmbak/备份里），
# 都能把原来的 renderer/nameservers 找回来。没装 python-yaml 时，renderer 还有 grep 兜底。
_CAPTURE_PY = """import sys, glob
try:
    import yaml
except Exception:
    print("RENDERER=''"); print("NS=''"); sys.exit(0)
iface = sys.argv[1]
files = (sorted(glob.glob('/etc/netplan/*.yaml'))
         + sorted(glob.glob('/etc/netplan/*.ipmbak'))
         + sorted(glob.glob('/tmp/netplan-bak/*.yaml')))
renderer = ''
ns = []
for f in files:
    try:
        with open(f) as fh:
            d = yaml.safe_load(fh) or {}
    except Exception:
        continue
    net = d.get('network', {}) or {}
    if not renderer and net.get('renderer'):
        renderer = str(net['renderer'])
    eth = (net.get('ethernets') or {}).get(iface, {}) or {}
    addrs = ((eth.get('nameservers') or {}).get('addresses')) or []
    if not ns and addrs:
        ns = [str(a) for a in addrs]
renderer = ''.join(c for c in renderer if c.isalnum())   # 只留字母数字，防注入
ns = ','.join(a for a in ns if all(c.isdigit() or c in '.:abcdefABCDEF' for c in a))
print("RENDERER='%s'" % renderer)
print("NS='%s'" % ns)
"""


def build_change_script(iface, new_ip, prefix="24", gateway=""):
    """生成「读原配置 -> 备份 -> 禁用旧配置 -> 写入新配置 -> 后台应用」的完整脚本文本。

    参数都来自界面表单；iface / new_ip 必填，缺了会抛 ValueError，
    由上层转成「请先填写…」的友好提示。

    默认网关是**强制**配置的（这样对方会持续 ARP 找网关、才能被扫描发现）：
      - 用户填了 gateway 就用用户的；
      - 用户留空则自动取该网段的默认网关（default_gateway_for，网络号 + 1）。

    另外会**保留原配置的 renderer 和 nameservers**（见 _CAPTURE_PY 的说明），
    避免把设备的网络管家从 NetworkManager 悄悄换成安静的 networkd、导致它扫不到。
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

    return "\n".join([
        "# 这些值来自界面表单，先放进 shell 变量，后面拼配置时用。",
        f"IFACE='{iface}'",
        f"NEWIP='{new_ip}'",
        f"PREFIX='{prefix}'",
        f"GATEWAY='{gateway}'",
        "",
        f"# 第①步 先删掉本工具上次可能写过的文件（避免它被当成“原配置”备份/禁用）",
        f"rm -f {DROPIN}",
        "",
        "# 第②步 读出对方原来的 renderer（谁管网络）和 nameservers（DNS），一会儿原样保留。",
        f"cat > /tmp/ipm_capture.py <<'PYEOF'\n{_CAPTURE_PY}PYEOF",
        'eval "$(python3 /tmp/ipm_capture.py "$IFACE" 2>/dev/null)"',
        "rm -f /tmp/ipm_capture.py",
        "# 兜底：python-yaml 缺失导致 RENDERER 为空时，用 grep 从原/备份文件里抠出 renderer。",
        'if [ -z "$RENDERER" ]; then',
        "  RENDERER=$(grep -h -oiE 'renderer:[[:space:]]*(networkmanager|networkd)' "
        "/etc/netplan/*.yaml /etc/netplan/*.ipmbak /tmp/netplan-bak/*.yaml 2>/dev/null "
        "| grep -oiE '(networkmanager|networkd)' | head -1)",
        "fi",
        'echo "将保留 renderer=${RENDERER:-(未设置,用 netplan 默认)} nameservers=${NS:-(无)}"',
        "",
        f"# 第③步 备份现有 netplan 配置到 {BACKUP_DIR}（此时只剩对方真正的原配置）",
        f"mkdir -p {BACKUP_DIR}",
        f"cp /etc/netplan/*.yaml {BACKUP_DIR}/ 2>/dev/null || true",
        "",
        "# 第④步 禁用原有配置：把每个 *.yaml 改名成 *.ipmbak，让 netplan 忽略它们。",
        "#        这是“真替换”的关键——否则旧文件里的旧 IP 会和新 IP 合并、一起残留。",
        "for f in /etc/netplan/*.yaml; do",
        '  [ -e "$f" ] || continue          # 没有任何 .yaml 时，通配符不展开，跳过',
        f'  mv "$f" "$f{DISABLED_SUFFIX}"',
        "done",
        "",
        f"# 第⑤步 写入新配置到 {DROPIN}（逐行拼装，好把上面保留的 renderer/nameservers 塞进去）",
        "{",
        '  echo "network:"',
        '  echo "  version: 2"',
        '  [ -n "$RENDERER" ] && echo "  renderer: $RENDERER"',
        '  echo "  ethernets:"',
        '  echo "    $IFACE:"',
        '  echo "      dhcp4: false"',
        '  echo "      addresses: [$NEWIP/$PREFIX]"',
        '  if [ -n "$GATEWAY" ]; then',
        '    echo "      routes:"',
        '    echo "        - to: default"',
        '    echo "          via: $GATEWAY"',
        "  fi",
        '  if [ -n "$NS" ]; then',
        '    echo "      nameservers:"',
        '    echo "        addresses: [$NS]"',
        "  fi",
        f"}} > {DROPIN}",
        f"chmod 600 {DROPIN}",
        f"echo '已写入 {DROPIN}：'; cat {DROPIN}",
        "",
        "# 第⑥步 后台应用（sleep 2 让这条 SSH 先干净返回；IP 一变当前连接就会断，属正常）",
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

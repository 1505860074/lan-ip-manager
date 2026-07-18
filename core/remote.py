"""通过 SSH 连接对方设备，并执行【单条】命令。

本模块只做两件事，刻意保持简单、没有任何“后台自动改配置”的黑盒逻辑：
  1. connect()      —— 用账号密码建立一条 SSH 连接；
  2. run_command()  —— 在这条连接上执行【一条】命令，把结果原样带回来。

设计原则：真正要在对方机器上跑的命令，全部由前端命令台明文给出、
经过用户审查后再发送。这里不替用户“聪明地”拼命令，只忠实执行并把
退出码 / 标准输出 / 标准错误返回给前端展示。

============================================================================
【关于改对方 IP】
  以前这里有一套自动读取 / 改写 / 应用 netplan yaml 的逻辑，现在改成了
  “预设命令”：前端把改 IP 需要执行的 shell 命令生成成文本填进命令台，
  你看清楚了再点发送。常见的几套 Linux 网络配置系统对应的命令思路：
    - netplan            ：写 /etc/netplan/*.yaml，再 `netplan apply`
    - ifupdown           ：改 /etc/network/interfaces，再 `systemctl restart networking`
    - NetworkManager     ：`nmcli con mod <连接> ipv4.addresses ...` 再 `nmcli con up`
    - systemd-networkd   ：写 /etc/systemd/network/*.network，再 `networkctl reload`
  前端命令面板目前内置了 netplan 一套；要支持别的，照着在前端加预设即可。
============================================================================
"""
import shlex

import paramiko


def connect(host, user, password, port=22, timeout=10):
    """建立 SSH 连接并返回 client 对象。"""
    cli = paramiko.SSHClient()
    # AutoAddPolicy：自动接受对方主机密钥。直连临时设备场景下够用；
    # 生产环境更严谨的做法是校验 known_hosts，这里为了好用先放宽。
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        host,
        port=port,
        username=user,
        password=password,
        timeout=timeout,
        allow_agent=False,     # 不用本机 ssh-agent 里的密钥
        look_for_keys=False,   # 不去 ~/.ssh 找密钥，只用我们给的密码
    )
    return cli


def run_command(cli, command, sudo_pw=None, timeout=30):
    """在远端执行一条命令，返回 (退出码, 标准输出, 标准错误)。

    sudo_pw 不为 None 时，以 root 身份执行：
        echo 密码 | sudo -S -p '' bash -c '真正的命令'
      -S    让 sudo 从标准输入读密码（而不是弹终端交互）；
      -p '' 把 sudo 的提示语设成空串，免得混进输出里。
    注意：这种模式下 command 里【不要】再自己写 sudo，交给这里统一提权即可。

    为什么用 bash -c 把整条命令包起来：这样命令里的管道、重定向、heredoc、
    后台 & 等都会在对方的 shell 里正常生效，而不是被 sudo 只当成第一个程序名。
    """
    if sudo_pw is not None:
        command = (
            f"echo {shlex.quote(sudo_pw)} | "
            f"sudo -S -p '' bash -c {shlex.quote(command)}"
        )
    stdin, stdout, stderr = cli.exec_command(command, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err

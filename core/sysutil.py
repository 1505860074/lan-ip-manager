"""本机侧的底层小工具：执行系统命令。

设计说明
--------
本工具需要用 root 权限运行（抓包、给网卡加临时 IP 都要 root），
所以这里直接调用系统自带的 `ip` 命令，不再额外套 sudo。
"""
import subprocess


def run_cmd(args, timeout=15):
    """执行一条本地命令并拿到结果。

    参数
      args    : 命令列表，例如 ["ip", "-j", "addr"]。
                用「列表」而不是「一整行字符串」，可以避免 shell 注入，
                也省得自己处理带空格的参数转义。
    返回
      (returncode, stdout, stderr)，三个都已经 decode 成字符串。
      returncode == 0 一般代表成功。
    """
    p = subprocess.run(args, capture_output=True, timeout=timeout)
    return (
        p.returncode,
        p.stdout.decode(errors="replace"),
        p.stderr.decode(errors="replace"),
    )

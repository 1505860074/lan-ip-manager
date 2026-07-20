"""本机侧的底层小工具：执行系统命令。

设计说明
--------
本工具需要用 root 权限运行（抓包、给网卡加临时 IP 都要 root），
所以这里直接调用系统自带的 `ip` 命令，不再额外套 sudo。
"""
import os
import subprocess
import sys


def clean_env():
    """给“调用系统命令的子进程”用的干净环境变量副本。

    为什么需要它
    ----------
    用 PyInstaller 打包成单文件后，程序运行时会把 LD_LIBRARY_PATH 指向自己的临时解压
    目录（好让内嵌的动态库被优先加载）。可一旦我们再去调用系统命令（ip、sudo、
    xdg-open、zenity…），这些系统程序也继承了这个变量，就会误加载我们打包目录里的库、
    导致加载失败或行为异常。PyInstaller 会把改动前的原始值存进 LD_LIBRARY_PATH_ORIG，
    这里据此把 LD_LIBRARY_PATH 还原（原本就没有则删掉），子进程才能正常运行。
    没打包（直接跑源码）时没有这个变量，函数原样返回环境副本，无副作用。
    """
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        orig = env.get("LD_LIBRARY_PATH_ORIG")
        if orig is not None:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


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
    p = subprocess.run(args, capture_output=True, timeout=timeout, env=clean_env())
    return (
        p.returncode,
        p.stdout.decode(errors="replace"),
        p.stderr.decode(errors="replace"),
    )

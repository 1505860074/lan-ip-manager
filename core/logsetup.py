"""集中配置本地日志：把运行时的「操作 / 发现设备 / 错误 / HTTP 访问」写进
项目根目录下的 logs/，并按文件大小自动滚动、超量自动删除。

为什么单独抽一个模块
--------------------
日志配置只应该在进程启动时做【一次】（重复 addHandler 会导致同一条日志被写多遍）。
所以这里提供 setup_logging()，由 app.py 在启动时调一次；其它代码只需
    import logging
    log = logging.getLogger("ipmanager")
    log.info("...")
就能往同一份日志里写，彼此不耦合。

「按时序维护」怎么做到
--------------------
每条日志都带 `年-月-日 时:分:秒` 时间戳，写入本身就是按发生顺序追加的，
所以文件天然是时间序。

「定期删除」怎么做到
------------------
用 RotatingFileHandler：单个文件写满 maxBytes 就把它改名成 app.log.1、
app.log.2 …… 再开一个新的空文件继续写；最多保留 backupCount 个历史文件，
再老的会被自动删掉。所以磁盘占用有上限，不会无限增长——这就是「定期删除」。
（它是按“大小”触发滚动，不是按“时间”；你之前选的就是按大小滚动。）
"""
import logging
import os
import pwd
import sys
from logging.handlers import RotatingFileHandler


def _base_dir():
    """算出日志目录该放在哪儿的“父目录”。

    分两种情况：
      - 打包成单文件后（sys.frozen 为真）：__file__ 在 PyInstaller 的临时解压目录里，
        程序一退出那目录就被删，日志会跟着没。所以日志要放在【可执行文件所在目录】旁边，
        才能持久保留（sys.executable 就是那个可执行文件本身的路径）。
      - 直接跑源码（开发时）：放在项目根目录（本文件 core/logsetup.py 往上跳两级）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 日志目录 = 上面算出的父目录下的 logs/。
LOG_DIR = os.path.join(_base_dir(), "logs")


def _chown_to_invoking_user(path):
    """把 path 的属主改回“当初用 sudo 启动本程序的那个普通用户”（环境变量 SUDO_USER）。

    程序以 root 运行时，它建的日志默认归 root，普通用户想读/删都得再 sudo，很别扭。
    这里在建好目录/文件后把属主改回原用户，方便用户直接管理自己的日志。
    只有“当前是 root 且确实是 sudo 提权来的”才改；否则什么都不做（也就不会报错）。
    """
    try:
        if os.geteuid() != 0:
            return
        sudo_user = os.environ.get("SUDO_USER")
        if not sudo_user or sudo_user == "root":
            return
        info = pwd.getpwnam(sudo_user)      # 查这个用户名对应的 uid/gid
        os.chown(path, info.pw_uid, info.pw_gid)
    except Exception:
        pass  # 改属主失败不影响功能，忽略即可

MAX_BYTES = 5 * 1024 * 1024   # 单个日志文件上限：5MB，超过就滚动
BACKUP_COUNT = 5              # app.log 最多留 5 个历史（连当前共 6 个 ≈ 30MB 封顶）
ACCESS_BACKUP_COUNT = 3       # 访问日志更吵（每秒 poll 一条），留少一点

# 进程级“只配一次”开关：重复调 setup_logging() 直接返回，避免重复挂 handler。
_configured = False


def read_access_log(offset=0, first_tail=8192):
    """从 logs/access.log 的 offset 字节处往后读，返回 (新增文本, 新的 offset)。

    直接读已有的日志文件（不另建日志系统）；前端把返回的 offset 存下来，下次带上，
    就能只取增量、像 `tail -f` 那样一直跟。几个边界都照顾到了：
      - 文件被滚动 / 截断（当前大小 < offset）：offset 归零，从头再读；
      - 首次（offset=0）且文件很大：只从末尾约 first_tail 字节开始，免得一次糊一大屏历史；
      - 只返回到最后一个换行为止的【完整】行，避免把正在写入的半行截断显示。
    """
    path = os.path.join(LOG_DIR, "access.log")
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", 0
    if offset > size:            # 文件滚动/截断了，从头来
        offset = 0
    with open(path, "rb") as f:
        if offset == 0 and size > first_tail:
            f.seek(size - first_tail)
            f.readline()         # 丢掉可能不完整的第一行
        else:
            f.seek(offset)
        start = f.tell()
        data = f.read()
    nl = data.rfind(b"\n")
    if nl == -1:                 # 还没有一整行新内容，等下次
        return "", start
    consumed = data[: nl + 1]
    return consumed.decode("utf-8", errors="replace"), start + len(consumed)


def _make_handler(filename, backup_count):
    """造一个「按大小滚动」的文件 handler，指向 logs/<filename>。"""
    # exist_ok=True：目录已存在也不报错；每次都保证目录在（首次运行会自动建）。
    os.makedirs(LOG_DIR, exist_ok=True)
    _chown_to_invoking_user(LOG_DIR)           # 目录属主改回启动程序的普通用户
    log_path = os.path.join(LOG_DIR, filename)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_BYTES,
        backupCount=backup_count,
        encoding="utf-8",   # 日志里有中文，显式指定 UTF-8 避免乱码
    )
    _chown_to_invoking_user(log_path)          # 日志文件属主也改回普通用户
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    return handler


def _make_console_handler():
    """造一个「往终端(stderr)实时打」的 handler，恢复以前在终端看日志的体验。

    为什么需要它
    ----------
    werkzeug（Flask 开发服务器）平时能在终端刷请求日志，靠的是它启动时【自己】
    给 werkzeug logger 挂一个 stderr handler；但它会先判断“该 logger 是否已有
    handler”，有就不挂了。我们给它加了写文件的 handler 后，它就不再挂自己的
    终端 handler，于是终端不再刷日志。所以这里我们主动补一个终端 handler。

    格式只用 %(message)s（不加时间戳前缀）：因为 werkzeug 的访问行本身已带
    时间戳，再加一层会重复啰嗦；文件里则保留完整时间戳，供事后翻查。
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def setup_logging():
    """在进程启动时调用一次：把日志接到 logs/ 下的滚动文件上。"""
    global _configured
    if _configured:
        return

    # 终端 handler：两个 logger 共用同一个，既进文件也实时吐终端。
    console = _make_console_handler()

    # 我们自己的应用日志：操作事件、发现的设备、错误异常，都走这个 logger。
    app_logger = logging.getLogger("ipmanager")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(_make_handler("app.log", BACKUP_COUNT))  # 进文件
    app_logger.addHandler(console)                                 # 同时吐终端
    # propagate=False：不要再往上传给 root logger，避免和其它 handler 重复打印。
    app_logger.propagate = False

    # Flask/Werkzeug 的 HTTP 访问日志：单独写 access.log，避免每秒的 poll 请求
    # 把 app.log 里真正有用的“操作/设备”记录很快冲滚没了。
    access_logger = logging.getLogger("werkzeug")
    access_logger.setLevel(logging.INFO)
    access_logger.addHandler(_make_handler("access.log", ACCESS_BACKUP_COUNT))  # 进文件
    access_logger.addHandler(console)                                           # 同时吐终端
    # 说明：给 werkzeug 挂了 handler 后，它就不会再自动挂自己的终端 handler，
    # 所以这里必须由我们主动挂上面的 console，终端才会继续实时刷请求日志。

    _configured = True

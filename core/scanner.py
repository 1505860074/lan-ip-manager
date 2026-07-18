"""持续扫描会话：后台线程一直抓包，前端每秒来取一次当前结果。

为什么不用「每秒重新扫一次」
--------------------------
每次 sniff 都要打开 / 关闭一次抓包套接字，反复重来会有间隙、容易丢包。
所以这里用 scapy 的 AsyncSniffer 在【后台线程】里持续抓包，把发现的设备
不断累加进内存；前端每秒调一次 poll() 取当前快照即可 —— 抓包是连续的、
不间断的，只是展示每秒刷新。

线程安全
--------
抓包在 AsyncSniffer 自己的线程里跑，会不断改 self._seen；poll() 在请求线程里
读 self._seen。所以用两把锁分工，避免「读到一半被改」以及停止时的死锁：
  - _data：只保护 self._seen 的读写，握持时间极短（每个包 / 每次快照）；
  - _ctl ：保护 sniffer 的启停生命周期。
关键约束：绝不在握着 _data 时去 stop() sniffer（stop 会 join 抓包线程，而该
线程可能正等着拿 _data，会死锁）。启停只用 _ctl，重置 seen 时才短暂拿一下 _data。
"""
import threading
import time

from scapy.all import AsyncSniffer

from .discovery import classify, update_seen

# 设备「消失」老化窗口（秒）：超过这么久没再听到某设备，就把它从列表里移除。
# 不能设太小——局域网广播是稀疏的（几秒~几十秒一个包），太小会导致列表疯狂闪烁。
SEEN_TTL = 20


class ScanSession:
    def __init__(self):
        self._ctl = threading.Lock()
        self._data = threading.Lock()
        self._sniffer = None
        self._seen = {}
        self._iface = None

    def start(self, iface, my_mac=None):
        """（重新）开始在 iface 上持续抓包。会先停掉上一次的会话、清空结果。"""
        with self._ctl:
            self._stop_sniffer()
            with self._data:
                self._seen = {}
                self._iface = iface
            seen = self._seen  # 闭包捕获当前这份 seen

            def handle(pkt):
                # 抓包线程回调：短暂拿 _data，把这个包累加进 seen
                with self._data:
                    update_seen(seen, pkt, my_mac)

            self._sniffer = AsyncSniffer(iface=iface, prn=handle, store=False)
            self._sniffer.start()

    def poll(self, ttl=SEEN_TTL):
        """返回当前「在线」设备快照：{devices, hidden_public, running, iface}。

        先剔除超过 ttl 秒没再听到的设备（它们算「消失」了），再分类返回。
        这样列表反映的是「当前还在」的设备，拔线的会在 ttl 秒后自动掉出去。
        """
        now = time.monotonic()
        with self._data:
            stale = [
                mac
                for mac, rec in self._seen.items()
                if now - rec.get("last_seen", now) > ttl
            ]
            for mac in stale:
                del self._seen[mac]
            result = classify(self._seen, now=now)
        with self._ctl:
            running = self._sniffer is not None and getattr(self._sniffer, "running", False)
        result["running"] = running
        result["iface"] = self._iface
        return result

    def stop(self):
        """停止后台抓包（结果仍保留，poll 还能取到最后一次快照）。"""
        with self._ctl:
            self._stop_sniffer()

    def _stop_sniffer(self):
        # 只在握着 _ctl（且不握 _data）时调用
        if self._sniffer is not None:
            try:
                self._sniffer.stop()
            except Exception:
                pass
            self._sniffer = None


# 整个进程共用一个会话（本工具是本机单用户的小面板，够用）。
session = ScanSession()

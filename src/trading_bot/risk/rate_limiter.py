"""限速器 + 流量监控。保护 Binance API 限额和服务器带宽。"""
from __future__ import annotations
import time
import threading
import logging
from collections import deque

logger = logging.getLogger(__name__)


class RateLimiter:
    """滑动窗口限速器"""

    def __init__(self, max_per_second: float = 2, burst: int = 5):
        self._max_rate = max_per_second
        self._burst = burst
        self._lock = threading.Lock()
        self._timestamps: deque[float] = deque()
        self._total_requests = 0

    def acquire(self) -> bool:
        """获取许可。返回 False 表示需要等待"""
        with self._lock:
            now = time.time()
            # 清除1秒前的时间戳
            while self._timestamps and self._timestamps[0] < now - 1.0:
                self._timestamps.popleft()

            if len(self._timestamps) < self._burst:
                self._timestamps.append(now)
                self._total_requests += 1
                return True

            # burst 用完，按速率限制
            expected_interval = 1.0 / self._max_rate
            if self._timestamps and now - self._timestamps[-1] >= expected_interval:
                self._timestamps.append(now)
                self._total_requests += 1
                return True

            return False

    def wait_and_acquire(self, timeout: float = 3.0) -> bool:
        """等待直到获取许可或超时"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.acquire():
                return True
            time.sleep(0.05)
        return False

    @property
    def requests_per_second(self) -> float:
        with self._lock:
            now = time.time()
            recent = sum(1 for t in self._timestamps if t > now - 1.0)
            return recent


class TrafficMonitor:
    """流量监控器"""

    def __init__(self, monthly_limit_gb: float = 1500):
        self._monthly_limit = monthly_limit_gb
        self._bytes_total = 0
        self._lock = threading.Lock()
        self._bytes_history: deque[tuple[float, int]] = deque()
        self._warnings = 0

    def record(self, bytes_count: int):
        with self._lock:
            self._bytes_total += bytes_count
            now = time.time()
            self._bytes_history.append((now, bytes_count))
            # 保留最近1小时
            while self._bytes_history and self._bytes_history[0][0] < now - 3600:
                self._bytes_history.popleft()

    @property
    def total_gb(self) -> float:
        return self._bytes_total / 1e9

    @property
    def hourly_mbps(self) -> float:
        with self._lock:
            now = time.time()
            recent_bytes = sum(b for t, b in self._bytes_history if t > now - 3600)
            return recent_bytes * 8 / 3600 / 1e6

    def check_thresholds(self) -> str:
        """返回当前级别: normal / warning / reduce / emergency / hard_limit"""
        gb = self.total_gb
        if gb > 1450: return "hard_limit"
        if gb > 1300: return "emergency"
        if gb > 1100: return "reduce"
        if gb > 900: self._warnings += 1; return "warning"
        return "normal"


# 全局实例
order_limiter = RateLimiter(max_per_second=2, burst=5)
traffic_monitor = TrafficMonitor(monthly_limit_gb=1500)

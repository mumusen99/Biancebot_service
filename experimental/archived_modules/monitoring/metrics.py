"""监控指标：延迟、健康、流量。"""
from __future__ import annotations
import time
import threading
import logging
from dataclasses import dataclass, field
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class LatencyStats:
    """延迟统计"""
    market_transport_ms: float = 0.0   # 交易所→本地
    local_processing_ms: float = 0.0   # 本地处理
    trigger_decision_ms: float = 0.0   # 触发判断
    queue_wait_ms: float = 0.0         # 队列等待
    order_round_trip_ms: float = 0.0   # 订单往返
    first_fill_ms: float = 0.0         # 首次成交
    unprotected_window_ms: float = 0.0 # 无保护窗口

    _samples: deque = field(default_factory=lambda: deque(maxlen=1000))

    def record(self, phase: str, duration_ms: float):
        self._samples.append((phase, duration_ms, time.time()))

    def summary(self) -> dict:
        if not self._samples:
            return {}
        by_phase = {}
        for phase, dur, _ in self._samples:
            if phase not in by_phase:
                by_phase[phase] = []
            by_phase[phase].append(dur)
        return {p: f'avg={sum(v)/len(v):.1f}ms max={max(v):.1f}ms' for p, v in by_phase.items()}


class HealthMonitor:
    """健康监控"""

    def __init__(self):
        self._start_time = time.time()
        self._errors: deque[tuple[float, str]] = deque(maxlen=100)
        self._warnings: deque[tuple[float, str]] = deque(maxlen=200)

    def record_error(self, msg: str):
        self._errors.append((time.time(), msg))

    def record_warning(self, msg: str):
        self._warnings.append((time.time(), msg))

    @property
    def uptime_s(self) -> float:
        return time.time() - self._start_time

    @property
    def error_count_1h(self) -> int:
        cutoff = time.time() - 3600
        return sum(1 for t, _ in self._errors if t > cutoff)

    def status(self) -> dict:
        return {
            'uptime_h': f'{self.uptime_s/3600:.1f}',
            'errors_1h': self.error_count_1h,
            'warnings_1h': sum(1 for t, _ in self._warnings if t > time.time() - 3600),
        }


class TrafficMonitor:
    """流量监控（增强版）"""

    class Level:
        NORMAL = "NORMAL"
        REDUCED = "REDUCED"
        RISK_ONLY = "RISK_ONLY"
        HALT = "HALT"

    def __init__(self, monthly_limit_gb: float = 1500):
        self._monthly_limit = monthly_limit_gb
        self._bytes_total: int = 0
        self._lock = threading.Lock()
        self._bytes_history: deque[tuple[float, int]] = deque()

    def record(self, byte_count: int):
        with self._lock:
            self._bytes_total += byte_count
            now = time.time()
            self._bytes_history.append((now, byte_count))
            while self._bytes_history and self._bytes_history[0][0] < now - 3600:
                self._bytes_history.popleft()

    @property
    def total_gb(self) -> float:
        return self._bytes_total / 1e9

    @property
    def hourly_mbps(self) -> float:
        with self._lock:
            now = time.time()
            recent = sum(b for t, b in self._bytes_history if t > now - 3600)
            return recent * 8 / 3600 / 1e6

    def check(self) -> str:
        gb = self.total_gb
        if gb > 1450: return self.Level.HALT
        if gb > 1300: return self.Level.RISK_ONLY
        if gb > 1100: return self.Level.REDUCED
        if gb > 900:
            logger.warning(f'traffic warning: {gb:.0f}GB')
            return self.Level.REDUCED
        return self.Level.NORMAL


# 全局实例
latency_stats = LatencyStats()
health_monitor = HealthMonitor()
traffic_monitor = TrafficMonitor()

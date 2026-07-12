"""执行优先级队列。P0=紧急止损 > P1=保护单 > P2=TP调整 > P3=撤单 > P4=新开仓。"""
from __future__ import annotations
import time
import threading
import logging
from enum import IntEnum
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ExecutionPriority(IntEnum):
    EMERGENCY_EXIT = 0
    PROTECTION = 1
    POSITION_ADJUST = 2
    CANCEL_ENTRY = 3
    NEW_ENTRY = 4


@dataclass(order=True)
class ExecutionTask:
    priority: int
    created_ns: int = field(compare=False)
    symbol: str = field(compare=False)
    role: str = field(compare=False)  # ENTRY / STOP / TP1 / TP2 / EMERGENCY
    func: Callable = field(compare=False)
    args: tuple = field(compare=False, default_factory=tuple)
    kwargs: dict = field(compare=False, default_factory=dict)
    request_id: str = field(compare=False, default="")

    def execute(self):
        return self.func(*self.args, **self.kwargs)


class ExecutionPriorityQueue:
    """线程安全优先级执行队列"""

    def __init__(self, max_per_second: float = 2, burst: int = 5):
        self._lock = threading.Lock()
        self._queue: list[ExecutionTask] = []
        self._timestamps: deque[float] = deque()
        self._max_rate = max_per_second
        self._burst = burst
        self._total_executed = 0
        self._total_dropped = 0
        self._in_flight: dict[str, bool] = {}  # symbol:role → True

    def submit(self, task: ExecutionTask) -> bool:
        """提交任务。返回 False 表示队列满被丢弃"""
        with self._lock:
            # 去重：同一symbol+role已有在途任务则拒绝
            key = f"{task.symbol}:{task.role}"
            if self._in_flight.get(key):
                logger.debug(f"duplicate task dropped: {key}")
                self._total_dropped += 1
                return False

            # 按优先级插入
            self._queue.append(task)
            self._queue.sort(key=lambda t: t.priority)
            self._in_flight[key] = True
            return True

    def submit_emergency(self, symbol: str, func: Callable, *args, **kwargs) -> bool:
        """紧急任务（绕过限速）"""
        task = ExecutionTask(
            priority=ExecutionPriority.EMERGENCY_EXIT,
            created_ns=time.monotonic_ns(),
            symbol=symbol, role="EMERGENCY",
            func=func, args=args, kwargs=kwargs,
        )
        return self.submit(task)

    def submit_protection(self, symbol: str, role: str, func: Callable, *args, **kwargs) -> bool:
        task = ExecutionTask(
            priority=ExecutionPriority.PROTECTION,
            created_ns=time.monotonic_ns(),
            symbol=symbol, role=role,
            func=func, args=args, kwargs=kwargs,
        )
        return self.submit(task)

    def submit_entry(self, symbol: str, func: Callable, *args, **kwargs) -> bool:
        task = ExecutionTask(
            priority=ExecutionPriority.NEW_ENTRY,
            created_ns=time.monotonic_ns(),
            symbol=symbol, role="ENTRY",
            func=func, args=args, kwargs=kwargs,
        )
        return self.submit(task)

    def _rate_limited(self) -> bool:
        """检查是否超过速率限制"""
        now = time.time()
        while self._timestamps and self._timestamps[0] < now - 1.0:
            self._timestamps.popleft()
        if len(self._timestamps) < self._burst:
            return False
        if self._timestamps and now - self._timestamps[-1] >= 1.0 / self._max_rate:
            return False
        return True

    def process_one(self) -> Optional[ExecutionTask]:
        """处理一个任务。返回执行的任务或 None"""
        with self._lock:
            if not self._queue:
                return None

            # 最高优先级（紧急）不受限速
            top = self._queue[0]
            if top.priority > ExecutionPriority.PROTECTION and self._rate_limited():
                return None

            task = self._queue.pop(0)
            key = f"{task.symbol}:{task.role}"

        try:
            task.execute()
            self._total_executed += 1
        except Exception:
            logger.exception(f"task failed: {task.symbol} {task.role}")
        finally:
            with self._lock:
                self._in_flight.pop(key, None)
                self._timestamps.append(time.time())

        return task

    def process_all(self, max_count: int = 10) -> int:
        """处理最多 max_count 个任务"""
        count = 0
        for _ in range(max_count):
            if self.process_one() is None:
                break
            count += 1
        return count

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    @property
    def in_flight_count(self) -> int:
        return len(self._in_flight)

    @property
    def stats(self) -> dict:
        return {
            "queue_size": self.queue_size,
            "in_flight": self.in_flight_count,
            "total_executed": self._total_executed,
            "total_dropped": self._total_dropped,
        }


# 全局实例
execution_queue = ExecutionPriorityQueue(max_per_second=2, burst=5)


class LatencyTracker:
    """延迟追踪器"""

    def __init__(self):
        self._events: dict[str, dict[str, int]] = {}  # request_id → {phase: ns}

    def mark(self, request_id: str, phase: str, ts_ns: int = None):
        if ts_ns is None:
            ts_ns = time.monotonic_ns()
        if request_id not in self._events:
            self._events[request_id] = {}
        self._events[request_id][phase] = ts_ns

    def duration_ms(self, request_id: str, start: str, end: str) -> Optional[float]:
        ev = self._events.get(request_id, {})
        t0, t1 = ev.get(start), ev.get(end)
        if t0 and t1:
            return (t1 - t0) / 1_000_000
        return None

    def cleanup(self, max_age_ns: int = 60_000_000_000):
        now = time.monotonic_ns()
        expired = [k for k, v in self._events.items()
                   if now - max(v.values()) > max_age_ns]
        for k in expired:
            del self._events[k]


latency_tracker = LatencyTracker()

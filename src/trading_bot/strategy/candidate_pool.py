"""候选池管理器。维护15-20个候选币，管理生命周期。"""
from __future__ import annotations
import time
import logging
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    symbol: str
    candidate_type: str = ""          # TREND_PULLBACK / RANGE_REVERSAL / etc
    direction_bias: str = "LONG"
    watch_start_time: float = field(default_factory=time.time)
    expiry_time: float = 0.0         # 过期时间戳
    trigger_state: str = "WAIT_PULLBACK"
    planned_entry: Optional[float] = None
    planned_stop: Optional[float] = None
    priority_score: float = 0.0
    pos_pct: float = 0.5
    vwap_dev: float = 0.0

    def is_expired(self) -> bool:
        return time.time() > self.expiry_time

    def age_seconds(self) -> float:
        return time.time() - self.watch_start_time


class CandidatePool:
    """候选池管理器"""

    MAX_DEFAULT = 15
    MAX_SYMBOLS = 20
    DEEP_WATCH_MAX = 5

    # TTL by signal type
    SIGNAL_TTL = {
        'MOMENTUM_SCALP': 15,
        'MOMENTUM_SECOND_ENTRY': 30,
        'RANGE_REVERSAL': 60,
        'FAKE_BREAKOUT_REVERSAL': 60,
        'TREND_PULLBACK': 180,
        'BREAKOUT_RETEST': 180,
    }

    def __init__(self):
        self._candidates: dict[str, Candidate] = {}
        self._deep_watch: set[str] = set()

    @property
    def size(self) -> int:
        return len(self._candidates)

    @property
    def symbols(self) -> list[str]:
        return list(self._candidates.keys())

    @property
    def deep_watch_symbols(self) -> list[str]:
        return list(self._deep_watch)

    def update(self, signals: list[dict], max_size: int = None):
        """用新信号刷新候选池"""
        max_size = max_size or self.MAX_DEFAULT

        # 清除过期
        expired = [s for s, c in self._candidates.items() if c.is_expired()]
        for s in expired:
            del self._candidates[s]
            self._deep_watch.discard(s)

        # 添加新信号
        for sig in signals:
            sym = sig.get('symbol', '')
            if not sym:
                continue
            trade_type = sig.get('trade_type', 'TREND_PULLBACK')
            ttl = self.SIGNAL_TTL.get(trade_type, 60)
            cand = Candidate(
                symbol=sym,
                candidate_type=trade_type,
                direction_bias=sig.get('side', 'LONG'),
                expiry_time=time.time() + ttl,
                trigger_state=sig.get('trigger_state', 'WAIT_PULLBACK'),
                planned_entry=sig.get('limit_price'),
                planned_stop=sig.get('sl_price'),
                priority_score=sig.get('score', 0),
                pos_pct=sig.get('pos_pct', 0.5),
                vwap_dev=sig.get('dist_vwap', 0),
            )
            self._candidates[sym] = cand

        # 只保留前 N 个
        if len(self._candidates) > self.MAX_SYMBOLS:
            sorted_by_score = sorted(
                self._candidates.items(),
                key=lambda x: x[1].priority_score, reverse=True
            )
            self._candidates = dict(sorted_by_score[:self.MAX_SYMBOLS])

    def promote_to_deep_watch(self, symbol: str):
        """提升到深度监控"""
        if len(self._deep_watch) < self.DEEP_WATCH_MAX:
            self._deep_watch.add(symbol)

    def demote_from_deep_watch(self, symbol: str):
        self._deep_watch.discard(symbol)

    def remove(self, symbol: str):
        self._candidates.pop(symbol, None)
        self._deep_watch.discard(symbol)

    def get(self, symbol: str) -> Optional[Candidate]:
        return self._candidates.get(symbol)

    def get_ready_to_trigger(self) -> list[Candidate]:
        return [c for c in self._candidates.values()
                if c.trigger_state == 'READY_TO_TRIGGER' and not c.is_expired()]


# 全局候选池
candidate_pool = CandidatePool()

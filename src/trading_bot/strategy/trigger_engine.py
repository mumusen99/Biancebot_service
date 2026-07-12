"""触发引擎：毫秒级事件驱动触发判断。"""
from __future__ import annotations
import time
import logging
from dataclasses import dataclass
from typing import Optional

from trading_bot.strategy.incremental_features import feature_store, IncrementalFeatures

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TriggerDecision:
    accepted: bool
    symbol: str
    side: str
    reason: str
    signal_ts_ns: int
    reference_price: float


class TriggerEngine:
    """毫秒级触发判断"""

    MIN_EVAL_INTERVAL_MS = 20   # 最快 20ms 评估一次
    FALLBACK_INTERVAL_MS = 100  # 最多 100ms 兜底评估
    MAX_SYMBOLS = 5

    def __init__(self):
        self._last_eval: dict[str, int] = {}  # symbol → timestamp_ns

    def evaluate(self, symbol: str, side: str, plan_entry: float,
                 plan_stop: float, expires_at_ns: int,
                 max_deviation_r: float = 0.15) -> TriggerDecision:
        """评估是否触发开仓"""

        now_ns = time.monotonic_ns()

        # 节流
        if symbol in self._last_eval:
            elapsed_ms = (now_ns - self._last_eval[symbol]) // 1_000_000
            if elapsed_ms < self.MIN_EVAL_INTERVAL_MS:
                return TriggerDecision(False, symbol, side, 'throttled', now_ns, 0)

        self._last_eval[symbol] = now_ns

        # 检查过期
        if now_ns > expires_at_ns:
            return TriggerDecision(False, symbol, side, 'expired', now_ns, 0)

        # 获取增量特征
        features: IncrementalFeatures = feature_store.get(symbol)
        if features.last_update_ns == 0:
            return TriggerDecision(False, symbol, side, 'no_data', now_ns, 0)

        current_price = features.last_price or features.micro_price
        if current_price <= 0:
            return TriggerDecision(False, symbol, side, 'no_price', now_ns, 0)

        # 价格偏离检查
        risk_dist = abs(plan_entry - plan_stop)
        if risk_dist > 0:
            deviation = abs(current_price - plan_entry)
            if deviation > max_deviation_r * risk_dist:
                return TriggerDecision(False, symbol, side,
                    f'price_deviation={deviation/risk_dist*100:.0f}%R', now_ns, current_price)

        # 点差检查
        if features.spread_bps > 10:
            return TriggerDecision(False, symbol, side,
                f'spread={features.spread_bps:.0f}bps', now_ns, current_price)

        # 限速检查（由执行队列处理）

        return TriggerDecision(True, symbol, side, 'ready', now_ns, current_price)

    def is_ready(self, symbol: str) -> bool:
        """候选是否就绪"""
        f = feature_store.get(symbol)
        return f.last_update_ns > 0 and not self._is_stale(symbol)

    def _is_stale(self, symbol: str, max_age_ms: int = 2000) -> bool:
        f = feature_store.get(symbol)
        if f.last_update_ns == 0:
            return True
        return (time.monotonic_ns() - f.last_update_ns) > max_age_ms * 1_000_000


trigger_engine = TriggerEngine()

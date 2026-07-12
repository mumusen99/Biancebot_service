"""持仓服务：生命周期管理 + 时间止损 + 微结构退出。"""
from __future__ import annotations
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    symbol: str
    side: str
    trade_type: str

    entry_price: float
    initial_qty: float
    remaining_qty: float

    stop_price: float
    tp1_price: float
    tp2_price: float

    tp1_hit: bool = False
    tp2_hit: bool = False
    runner_active: bool = False

    entry_monotonic_ns: int = field(default_factory=time.monotonic_ns)
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0

    # 保护单状态
    stop_order_id: str = ""
    tp1_order_id: str = ""
    tp2_order_id: str = ""

    @property
    def hold_ms(self) -> int:
        return (time.monotonic_ns() - self.entry_monotonic_ns) // 1_000_000

    @property
    def hold_s(self) -> float:
        return self.hold_ms / 1000.0

    def update_mfe_mae(self, current_price: float):
        if self.side == 'LONG':
            excursion = (current_price - self.entry_price) / self.entry_price
        else:
            excursion = (self.entry_price - current_price) / self.entry_price
        self.max_favorable_excursion = max(self.max_favorable_excursion, excursion)
        self.max_adverse_excursion = min(self.max_adverse_excursion, excursion)


class TimeStop:
    """时间止损规则"""

    WARNING_MS = 30_000      # 30秒警戒
    REDUCE_MS = 60_000       # 60秒减仓
    EXIT_MIN_MS = 90_000     # 90秒退出
    EXIT_MAX_MS = 120_000    # 120秒强制退出

    @classmethod
    def check(cls, pos: PositionState) -> Optional[str]:
        """返回 None=正常, 否则返回动作: WARN / REDUCE / EXIT"""
        ms = pos.hold_ms
        risk_dist = abs(pos.entry_price - pos.stop_price) if pos.stop_price > 0 else pos.entry_price * 0.01
        mfe_r = pos.max_favorable_excursion / risk_dist * pos.entry_price if risk_dist > 0 else 0

        if ms > cls.EXIT_MAX_MS:
            return 'EXIT'
        if ms > cls.EXIT_MIN_MS and mfe_r < 0.20:
            return 'EXIT'
        if ms > cls.REDUCE_MS and mfe_r < 0.15:
            return 'REDUCE'
        if ms > cls.WARNING_MS and mfe_r < 0.05:
            return 'WARN'
        return None


class MicroExitEngine:
    """微结构退出：基于盘口和订单流"""

    @classmethod
    def should_exit(cls, pos: PositionState, bs_ratio: float = 1.0,
                    book_imbalance: float = 0.0, micro_price: float = 0.0) -> bool:
        """判断是否应该微结构退出"""
        if pos.side == 'LONG':
            # 多单：卖压加重
            if bs_ratio < 0.5 and book_imbalance < -0.3:
                return True
            if micro_price > 0 and micro_price < pos.stop_price:
                return True
        else:
            # 空单：买压加重
            if bs_ratio > 2.0 and book_imbalance > 0.3:
                return True
            if micro_price > 0 and micro_price > pos.stop_price * 1.02:
                return True
        return False


class PositionService:
    """持仓服务：统一管理所有持仓"""

    def __init__(self):
        self._positions: dict[str, PositionState] = {}  # symbol:side → position

    def add(self, pos: PositionState):
        key = f"{pos.symbol}:{pos.side}"
        self._positions[key] = pos

    def remove(self, symbol: str, side: str):
        key = f"{symbol}:{side}"
        self._positions.pop(key, None)

    def get(self, symbol: str, side: str) -> Optional[PositionState]:
        return self._positions.get(f"{symbol}:{side}")

    def all_active(self) -> list[PositionState]:
        return [p for p in self._positions.values() if p.remaining_qty > 0]

    def on_tp1_hit(self, symbol: str, side: str):
        pos = self.get(symbol, side)
        if pos:
            pos.tp1_hit = True
            pos.remaining_qty = pos.initial_qty * 0.50  # 还剩50%+20%
            pos.stop_price = pos.entry_price  # 移到保本

    def on_tp2_hit(self, symbol: str, side: str):
        pos = self.get(symbol, side)
        if pos:
            pos.tp2_hit = True
            pos.runner_active = True
            pos.remaining_qty = pos.initial_qty * 0.20
            # 锁定利润
            if pos.side == 'LONG':
                pos.stop_price = pos.entry_price + abs(pos.entry_price - pos.stop_price) * 0.15
            else:
                pos.stop_price = pos.entry_price - abs(pos.entry_price - pos.stop_price) * 0.15

    def on_stop_hit(self, symbol: str, side: str):
        self.remove(symbol, side)

    @property
    def count(self) -> int:
        return len([p for p in self._positions.values() if p.remaining_qty > 0])


position_service = PositionService()

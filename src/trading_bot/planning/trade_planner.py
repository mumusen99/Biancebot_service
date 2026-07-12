"""TradePlan: 将 SignalCandidate 转换为可执行的交易计划。"""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
import logging

from trading_bot.strategy.trade_router import get_stop_rule

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradePlan:
    plan_id: str
    symbol: str
    side: str
    trade_type: str

    quantity: float

    entry_type: str  # MARKET / LIMIT / LIMIT_IOC
    entry_price: float | None
    max_entry_price: float

    stop_price: float
    tp1_price: float
    tp2_price: float

    tp1_ratio: float = 0.50
    tp2_ratio: float = 0.30
    runner_ratio: float = 0.20

    time_stop_ms: int = 60000  # 60秒
    risk_amount: float = 0.0
    max_slippage_bps: float = 5.0

    created_at_ns: int = field(default_factory=time.monotonic_ns)

    @property
    def tp1_qty(self) -> float:
        return self.quantity * self.tp1_ratio

    @property
    def tp2_qty(self) -> float:
        return self.quantity * self.tp2_ratio

    @property
    def runner_qty(self) -> float:
        return self.quantity * self.runner_ratio


class TradePlanner:
    """将候选信号 + 风控参数 → TradePlan"""

    def __init__(self, equity: float = 200.0, risk_pct: float = 0.002):
        self.equity = equity
        self.risk_pct = risk_pct  # 0.2% per trade

    def plan(self, candidate: dict, confidence_factor: float = 1.0) -> TradePlan | None:
        """生成交易计划"""
        sym = candidate.get('symbol', '')
        side = candidate.get('side', '')
        trade_type = candidate.get('trade_type', 'TREND_PULLBACK')
        entry_price = candidate.get('limit_price') or candidate.get('reference_price', 0)
        sl_price = candidate.get('sl_price', 0)

        if not entry_price or not sl_price:
            logger.warning(f'{sym} missing price data')
            return None

        stop_rule = get_stop_rule(trade_type)
        risk_dist = abs(entry_price - sl_price)
        risk_pct_abs = risk_dist / entry_price * 100

        # 检查止损范围
        if risk_pct_abs < stop_rule['min']:
            risk_pct_abs = stop_rule['min']
        if risk_pct_abs > stop_rule['max']:
            logger.warning(f'{sym} stop too wide: {risk_pct_abs:.2f}% > {stop_rule["max"]}%')
            return None

        # 仓位计算
        risk_per_trade = min(0.50, self.equity * self.risk_pct) * confidence_factor
        position_notional = min(risk_per_trade / (risk_pct_abs / 100), self.equity * 0.15)

        # TP 价格
        entry = float(entry_price)
        stop = float(sl_price)
        risk_dist = abs(entry - stop)

        if side == 'LONG':
            tp1 = entry + risk_dist * 0.6
            tp2 = entry + risk_dist * 1.2
        else:
            tp1 = entry - risk_dist * 0.6
            tp2 = entry - risk_dist * 1.2

        plan_id = f"PLAN-{sym}-{int(time.time())}-{uuid.uuid4().hex[:6]}"

        return TradePlan(
            plan_id=plan_id,
            symbol=sym, side=side, trade_type=trade_type,
            quantity=position_notional / entry,
            entry_type='MARKET' if candidate.get('tier') == 'market' else 'LIMIT',
            entry_price=entry,
            max_entry_price=entry * (1.005 if side == 'LONG' else 0.995),
            stop_price=stop,
            tp1_price=round(tp1, 8),
            tp2_price=round(tp2, 8),
            risk_amount=position_notional * risk_pct_abs / 100,
        )


trade_planner = TradePlanner()

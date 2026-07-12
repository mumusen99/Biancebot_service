"""风险引擎：统一审批开仓、减仓、拒绝。"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class RiskDecision(Enum):
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"


@dataclass
class RiskResult:
    decision: RiskDecision
    reason: str = ""
    risk_factor: float = 1.0  # 1.0=正常, <1.0=降仓


class RiskEngine:
    """统一风控审批"""

    def __init__(self):
        self.max_positions = 3
        self.max_daily_loss = 5.0
        self.max_consecutive_losses = 4
        self.cooldown_minutes = 30
        self.per_trade_risk_pct = 0.2
        self.correlation_limit = 0.7

        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._last_loss_time = 0.0
        self._current_positions = 0

    def approve_entry(self, trade_type: str, risk_amount: float,
                      confidence: float = 1.0, correlation: float = 0.0) -> RiskResult:
        """审批开仓请求"""

        # 仓位上限
        if self._current_positions >= self.max_positions:
            return RiskResult(RiskDecision.REJECT, f"max positions ({self.max_positions})")

        # 日内亏损上限
        if self._daily_pnl <= -self.max_daily_loss:
            return RiskResult(RiskDecision.REJECT, f"daily loss limit ({self.max_daily_loss})")

        # 连续亏损冷却
        if self._consecutive_losses >= self.max_consecutive_losses:
            import time
            cooling = self._last_loss_time + self.cooldown_minutes * 60 - time.time()
            if cooling > 0:
                return RiskResult(RiskDecision.REJECT,
                                  f"cooldown {self._consecutive_losses} losses, {cooling:.0f}s left")

        # 相关性限制
        if abs(correlation) > self.correlation_limit:
            return RiskResult(RiskDecision.REJECT, f"correlation={correlation:.2f}")

        # 行情信心降仓
        if confidence < 0.35:
            return RiskResult(RiskDecision.REJECT, f"regime confidence={confidence:.2f}")
        elif confidence < 0.50:
            return RiskResult(RiskDecision.REDUCE, f"low confidence", 0.4)
        elif confidence < 0.70:
            return RiskResult(RiskDecision.REDUCE, f"moderate confidence", 0.7)

        return RiskResult(RiskDecision.APPROVE, "ok")

    def record_trade(self, pnl: float):
        """记录交易结果"""
        self._daily_pnl += pnl
        if pnl < 0:
            self._consecutive_losses += 1
            import time
            self._last_loss_time = time.time()
        elif pnl > 0:
            self._consecutive_losses = 0

    def update_positions(self, count: int):
        self._current_positions = count

    def reset_daily(self):
        self._daily_pnl = 0.0
        self._consecutive_losses = 0


risk_engine = RiskEngine()

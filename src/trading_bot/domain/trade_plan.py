"""Unified TradePlan — single source of truth for all trade data. Immutable after risk approval."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class TakeProfitLevel:
    """A single take-profit level.

    close_fraction_of_original: fraction of ORIGINAL position to close at this level.
      0.50 means 50% of original qty. Sum of all levels should be ≤ 1.0.
    """
    price: Decimal
    close_fraction_of_original: Decimal
    role: str  # "TP1" | "TP2" | "RUNNER"

    def __post_init__(self):
        if not (0 < self.close_fraction_of_original <= 1):
            raise ValueError(f"fraction must be 0-1, got {self.close_fraction_of_original}")


@dataclass(frozen=True, slots=True)
class TradePlan:
    """Immutable trade plan — frozen after RiskEngine approval. Execution layer only accepts this."""

    plan_id: str = field(default_factory=lambda: uuid4().hex[:12])
    symbol: str = ""
    side: str = ""  # "LONG" | "SHORT"

    # Entry parameters
    entry_price: Decimal = Decimal("0")
    order_type: str = "MARKET"  # "MARKET" | "LIMIT"
    limit_price: Optional[Decimal] = None

    # Risk parameters (frozen after approval)
    initial_stop_price: Decimal = Decimal("0")
    risk_per_unit: Decimal = Decimal("0")
    approved_risk_usdt: Decimal = Decimal("0")

    # Position sizing
    quantity: Decimal = Decimal("0")
    notional_usdt: Decimal = Decimal("0")
    leverage: int = 5
    margin_usdt: Decimal = Decimal("0")

    # Exit plan
    take_profit_levels: tuple = ()
    time_stop_seconds: int = 1500
    trailing_activate_r: Decimal = Decimal("2.5")
    trailing_distance_r: Decimal = Decimal("0.5")

    # Metadata
    trade_type: str = "TREND_PULLBACK"
    signal_score: float = 0.0
    signal_reason: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self):
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.approved_risk_usdt < 0:
            raise ValueError("approved_risk_usdt cannot be negative")


# Standard take-profit plans by trade type
TP_PLANS = {
    "TREND_PULLBACK": (
        TakeProfitLevel(Decimal("0"), Decimal("0.50"), "TP1"),
        TakeProfitLevel(Decimal("0"), Decimal("0.30"), "TP2"),
        # 20% runner (implicit, handled by trailing stop)
    ),
    "MOMENTUM_SCALP": (
        TakeProfitLevel(Decimal("0"), Decimal("0.50"), "TP1"),
        TakeProfitLevel(Decimal("0"), Decimal("0.30"), "TP2"),
    ),
    "RANGE_REVERSAL": (
        TakeProfitLevel(Decimal("0"), Decimal("0.50"), "TP1"),
        TakeProfitLevel(Decimal("0"), Decimal("0.50"), "TP2"),
        # no runner
    ),
}

"""Clear take-profit semantics — fractions of original quantity, not ambiguous tuples."""
from dataclasses import dataclass
from decimal import Decimal


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

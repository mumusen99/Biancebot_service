"""Position size calculator — Decimal-based, exchange-rule-aware, never exceeds approved risk."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional


@dataclass(frozen=True)
class PositionSizeResult:
    accepted: bool
    quantity: Decimal
    notional: Decimal
    expected_loss_usdt: Decimal
    reject_reason: Optional[str] = None


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Floor value to step size."""
    if step <= 0:
        return value
    return (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def calculate_position_size(
    *,
    approved_risk_usdt: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    step_size: Decimal,
    min_quantity: Decimal = Decimal("0"),
    min_notional: Decimal = Decimal("5"),
    max_notional: Decimal = Decimal("100"),  # 20U × 5x
    leverage: int = 5,
) -> PositionSizeResult:
    """Calculate position size from approved risk, never exceeding risk budget.

    Args:
        approved_risk_usdt: Max loss allowed for this trade in USDT
        entry_price: Planned entry price
        stop_price: Hard stop price
        step_size: Exchange step size for this symbol
        min_quantity: Minimum order quantity
        min_notional: Minimum notional value (USDT)
        max_notional: Maximum notional (margin × leverage)
        leverage: Leverage used

    Returns:
        PositionSizeResult with accepted=True/False
    """
    risk_per_unit = abs(entry_price - stop_price)
    if risk_per_unit <= 0:
        return PositionSizeResult(False, Decimal("0"), Decimal("0"), Decimal("0"), "INVALID_STOP")

    # Raw quantity from risk budget
    margin_cap = max_notional / Decimal(str(leverage))
    capped_risk = min(approved_risk_usdt, margin_cap * Decimal("0.01"))  # 1% of margin
    raw_qty = capped_risk / risk_per_unit
    qty = floor_to_step(raw_qty, step_size)

    if qty <= 0:
        return PositionSizeResult(False, Decimal("0"), Decimal("0"), Decimal("0"), "QTY_ZERO_AFTER_STEP")

    notional = qty * entry_price

    if qty < min_quantity or notional < min_notional:
        return PositionSizeResult(False, qty, notional, Decimal("0"), "BELOW_MIN")

    if notional > max_notional:
        qty = floor_to_step(max_notional / entry_price, step_size)
        notional = qty * entry_price

    actual_loss = qty * risk_per_unit

    # Final safety: reduce qty until loss ≤ approved risk
    while actual_loss > approved_risk_usdt and qty > step_size:
        qty -= step_size
        actual_loss = qty * risk_per_unit

    if qty <= 0:
        return PositionSizeResult(False, Decimal("0"), Decimal("0"), Decimal("0"), "RISK_BUDGET_EXHAUSTED")

    notional = qty * entry_price
    return PositionSizeResult(
        accepted=qty >= min_quantity and notional >= min_notional,
        quantity=qty,
        notional=notional,
        expected_loss_usdt=actual_loss,
    )

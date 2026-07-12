"""Initial stop calculator — floor algorithm: stop distance = max(structure+ATR, ATR floor, cost, minimum).

Principle: stop position determines position size, never compress stop to fit size."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from trading_bot.domain.trade_type import TradeType


@dataclass(frozen=True)
class InitialStopResult:
    accepted: bool
    stop_price: Optional[Decimal]
    stop_distance_pct: Decimal
    risk_per_unit: Decimal
    reject_reason: Optional[str] = None


# Per-trade-type stop configuration
STOP_CONFIG = {
    TradeType.TREND_PULLBACK: {
        "structure_buffer_atr_1m": Decimal("0.3"),
        "structure_buffer_atr_5m": Decimal("0.5"),
        "atr_multiplier": Decimal("0.8"),
        "minimum_stop_pct": Decimal("0.0035"),   # 0.35%
        "maximum_stop_pct": Decimal("0.009"),     # 0.90%
    },
    TradeType.RANGE_REVERSAL: {
        "structure_buffer_atr_1m": Decimal("0.3"),
        "structure_buffer_atr_5m": Decimal("0.4"),
        "atr_multiplier": Decimal("0.6"),
        "minimum_stop_pct": Decimal("0.0018"),   # 0.18%
        "maximum_stop_pct": Decimal("0.0045"),    # 0.45%
    },
    TradeType.MOMENTUM_SCALP: {
        "structure_buffer_atr_1m": Decimal("0.2"),
        "structure_buffer_atr_5m": Decimal("0.3"),
        "atr_multiplier": Decimal("0.5"),
        "minimum_stop_pct": Decimal("0.0012"),   # 0.12%
        "maximum_stop_pct": Decimal("0.0035"),    # 0.35%
    },
}


class InitialStopCalculator:
    """Calculate initial stop using floor algorithm."""

    @staticmethod
    def calculate(
        *,
        side: str,  # "LONG" | "SHORT"
        entry: Decimal,
        confirmed_structure_price: Decimal,
        atr_1m: Decimal,
        atr_5m: Decimal,
        tick_size: Decimal,
        cost_pct: Decimal = Decimal("0.0008"),  # 0.08% round-trip
        trade_type: TradeType = TradeType.TREND_PULLBACK,
    ) -> InitialStopResult:
        config = STOP_CONFIG.get(trade_type, STOP_CONFIG[TradeType.TREND_PULLBACK])

        # Structure buffer
        structure_buffer = max(
            atr_1m * config["structure_buffer_atr_1m"],
            atr_5m * config["structure_buffer_atr_5m"],
        )

        if side == "LONG":
            structure_stop = confirmed_structure_price - structure_buffer
            structure_distance = (entry - structure_stop) / entry
        else:
            structure_stop = confirmed_structure_price + structure_buffer
            structure_distance = (structure_stop - entry) / entry

        # Alternative anchors
        atr_distance = atr_1m / entry * config["atr_multiplier"]
        noise_buffer = Decimal("0.0002")  # 0.02% noise floor

        # Floor distance = max of all anchors
        distance = max(
            structure_distance,
            atr_distance,
            cost_pct + noise_buffer,
            config["minimum_stop_pct"],
        )

        if distance > config["maximum_stop_pct"]:
            return InitialStopResult(
                accepted=False,
                stop_price=None,
                stop_distance_pct=distance,
                risk_per_unit=Decimal("0"),
                reject_reason=f"STOP_TOO_WIDE: {float(distance)*100:.2f}% > {float(config['maximum_stop_pct'])*100:.2f}%",
            )

        # Calculate stop price
        if side == "LONG":
            raw_stop = entry * (Decimal("1") - distance)
        else:
            raw_stop = entry * (Decimal("1") + distance)

        # Align to tick size
        stop = (raw_stop / tick_size).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick_size

        risk_per_unit = abs(entry - stop)
        actual_distance = risk_per_unit / entry

        return InitialStopResult(
            accepted=True,
            stop_price=stop,
            stop_distance_pct=actual_distance,
            risk_per_unit=risk_per_unit,
        )

"""Unified TradeType enum — single source of truth for all trade type routing."""
from enum import Enum


class TradeType(str, Enum):
    TREND_PULLBACK = "TREND_PULLBACK"
    RANGE_REVERSAL = "RANGE_REVERSAL"
    MOMENTUM_SCALP = "MOMENTUM_SCALP"
    BREAKOUT_RETEST = "BREAKOUT_RETEST"
    FAILED_BREAKOUT_REVERSAL = "FAILED_BREAKOUT_REVERSAL"
    REBOUND_SHORT = "REBOUND_SHORT"
    CONTINUATION_SHORT = "CONTINUATION_SHORT"

    @classmethod
    def _missing_(cls, value):
        """Tolerate legacy string values."""
        if isinstance(value, str):
            for member in cls:
                if member.value == value:
                    return member
        return None

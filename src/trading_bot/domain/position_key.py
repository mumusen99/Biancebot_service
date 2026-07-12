"""Position key — unique identifier for exchange positions in hedge mode."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PositionKey:
    symbol: str
    side: str  # "LONG" | "SHORT"

    def __str__(self) -> str:
        return f"{self.symbol}:{self.side}"

    @classmethod
    def from_position(cls, pos: dict) -> "PositionKey":
        return cls(
            symbol=str(pos.get("symbol", "")),
            side=str(pos.get("positionSide", pos.get("side", "LONG"))).upper(),
        )

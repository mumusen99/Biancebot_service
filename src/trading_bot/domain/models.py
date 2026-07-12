"""领域模型：dataclass 定义，禁止 dict 传递。"""
from dataclasses import dataclass, field
import time
from typing import Optional


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    trade_type: str = ''
    direction_score: float = 0.0
    location_score: float = 0.0
    trigger_score: float = 0.0
    execution_score: float = 0.0
    final_score: float = 0.0
    reference_price: float = 0.0
    pos_pct: float = 0.5
    created_at_ns: int = field(default_factory=time.monotonic_ns)

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol, 'side': self.side, 'trade_type': self.trade_type,
            'dir_score': self.direction_score, 'loc_score': self.location_score,
            'trig_score': self.trigger_score, 'exec_score': self.execution_score,
            'score': self.final_score, 'pos_pct': self.pos_pct,
            'reference_price': self.reference_price,
        }


@dataclass(frozen=True)
class Order:
    order_id: str = ''
    client_order_id: str = ''
    symbol: str = ''
    side: str = ''
    order_type: str = ''
    quantity: float = 0.0
    price: float = 0.0
    role: str = ''  # ENTRY / STOP / TP1 / TP2 / RUNNER_STOP / EMERGENCY_EXIT


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    consecutive_losses: int = 0
    last_loss_time: float = 0.0
    current_positions: int = 0
    cooldown_active: bool = False


@dataclass(frozen=True, slots=True)
class PositionKey:
    """仓位唯一键 — symbol:side，支持双向持仓。"""
    symbol: str
    side: str  # LONG or SHORT

    def __post_init__(self):
        if self.side not in ("LONG", "SHORT"):
            raise ValueError(f"invalid position side: {self.side}")

    def serialize(self) -> str:
        """格式: BTCUSDT:LONG"""
        return f"{self.symbol}:{self.side}"

    @classmethod
    def deserialize(cls, key: str) -> "PositionKey":
        """从 'BTCUSDT:LONG' 反序列化。"""
        symbol, side = key.rsplit(":", 1)
        return cls(symbol=symbol, side=side)

    @classmethod
    def from_position(cls, pos: dict) -> "PositionKey":
        """从持仓字典提取键。"""
        return cls(
            symbol=str(pos.get("symbol", "")).upper(),
            side=str(pos.get("side", "LONG")).upper(),
        )

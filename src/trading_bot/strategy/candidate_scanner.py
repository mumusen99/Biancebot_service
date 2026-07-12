"""候选扫描器：扫描全市场，生成候选池。"""
from __future__ import annotations
import logging
from typing import Optional
from dataclasses import dataclass, field
import time

logger = logging.getLogger(__name__)


@dataclass
class SignalCandidate:
    """候选信号"""
    symbol: str
    side: str
    trade_type: str = ''

    direction_score: float = 0.0
    location_score: float = 0.0
    trigger_score: float = 0.0
    execution_score: float = 0.0

    regime_probabilities: dict = field(default_factory=dict)
    final_score: float = 0.0

    reference_price: float = 0.0
    pos_pct: float = 0.5
    created_at_ns: int = field(default_factory=time.monotonic_ns)
    expires_at_ns: int = 0

    # 订单相关
    limit_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    direction_risk_factor: float = 1.0
    stop_min: float = 0.35
    stop_max: float = 0.90

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol, 'side': self.side,
            'trade_type': self.trade_type,
            'dir_score': self.direction_score,
            'loc_score': self.location_score,
            'trig_score': self.trigger_score,
            'exec_score': self.execution_score,
            'score': self.final_score,
            'pos_pct': self.pos_pct,
            'limit_price': self.limit_price,
            'sl_price': self.sl_price,
            'tp_price': self.tp_price,
            'direction_risk_factor': self.direction_risk_factor,
            'stop_min': self.stop_min,
            'stop_max': self.stop_max,
        }


class CandidateScanner:
    """候选扫描器"""

    def __init__(self, universe_size: int = 100, max_candidates: int = 20):
        self.universe_size = universe_size
        self.max_candidates = max_candidates
        self._candidates: dict[str, SignalCandidate] = {}

    def scan(self, signals: list[dict], btc_env: dict) -> list[SignalCandidate]:
        """扫描信号列表，返回通过初筛的候选"""
        from trading_bot.strategy.trade_router import route_trade_type
        candidates = []
        for sig in signals[:self.max_candidates]:
            sym = sig.get('symbol', '')
            side = sig.get('side', '')
            pos_pct = sig.get('pos_pct', 0.5)
            regime = btc_env.get('regime', 'unknown')

            trade_type = route_trade_type(regime, pos_pct)
            if not trade_type:
                continue

            c = SignalCandidate(
                symbol=sym, side=side, trade_type=trade_type,
                direction_score=sig.get('dir_score', 0),
                location_score=sig.get('loc_score', 0),
                trigger_score=sig.get('trig_score', 0),
                execution_score=sig.get('exec_score', 0),
                final_score=sig.get('score', 0),
                pos_pct=pos_pct,
                reference_price=sig.get('limit_price', 0),
                limit_price=sig.get('limit_price'),
                sl_price=sig.get('sl_price'),
                tp_price=sig.get('tp_price'),
            )
            candidates.append(c)
            self._candidates[sym] = c

        return candidates

    @property
    def symbols(self) -> list[str]:
        return list(self._candidates.keys())


candidate_scanner = CandidateScanner()

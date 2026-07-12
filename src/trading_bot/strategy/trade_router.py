"""交易路由器：根据市场状态 + 位置百分位分类交易类型。"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# 六种交易类型
VALID_TYPES = (
    'TREND_PULLBACK',
    'BREAKOUT_RETEST',
    'RANGE_REVERSAL',
    'MOMENTUM_SCALP',
    'MOMENTUM_SECOND_ENTRY',
    'FAILED_BREAKOUT',
)

# 按交易模式四维门槛
# ── 四维门槛 (match scalper.py tuned values)
THRESHOLDS = {
    'TREND_PULLBACK':          {'dir': 4.5, 'loc': 3.5, 'trig': 4.0, 'exec': 4.0},
    'BREAKOUT_RETEST':         {'dir': 6.0, 'loc': 4.5, 'trig': 6.0, 'exec': 5.0},
    'RANGE_REVERSAL':          {'dir': 3.0, 'loc': 5.0, 'trig': 4.5, 'exec': 4.0},
    'MOMENTUM_SCALP':          {'dir': 4.5, 'loc': 3.0, 'trig': 5.5, 'exec': 5.0},
    'MOMENTUM_SECOND_ENTRY':   {'dir': 5.0, 'loc': 5.0, 'trig': 6.0, 'exec': 5.5},
    'FAILED_BREAKOUT':         {'dir': 5.0, 'loc': 5.5, 'trig': 6.0, 'exec': 5.0},
}

# 止损范围
STOP_RULES = {
    'TREND_PULLBACK':          {'min': 0.35, 'max': 0.90},
    'BREAKOUT_RETEST':         {'min': 0.25, 'max': 0.70},
    'RANGE_REVERSAL':          {'min': 0.18, 'max': 0.45},
    'MOMENTUM_SCALP':          {'min': 0.12, 'max': 0.35},
    'MOMENTUM_SECOND_ENTRY':   {'min': 0.15, 'max': 0.40},
    'FAILED_BREAKOUT':         {'min': 0.20, 'max': 0.55},
}

# 信号 TTL（秒）
SIGNAL_TTL = {
    'MOMENTUM_SCALP': 15,
    'MOMENTUM_SECOND_ENTRY': 30,
    'RANGE_REVERSAL': 60,
    'FAILED_BREAKOUT': 60,
    'TREND_PULLBACK': 180,
    'BREAKOUT_RETEST': 180,
}


def route_trade_type(regime: str, pos_pct: float) -> Optional[str]:
    """根据市场状态和位置百分位路由交易类型"""
    is_trend = regime in ('strong_bull', 'bull', 'mild_bull',
                          'strong_bear', 'bear', 'mild_bear')
    is_range = regime in ('range', 'CHOP', 'unknown')

    if is_trend:
        if 0.20 <= pos_pct <= 0.60:
            return 'TREND_PULLBACK'
        elif pos_pct < 0.20:
            return 'RANGE_REVERSAL'
        else:
            return 'MOMENTUM_SCALP'
    elif is_range:
        if pos_pct <= 0.30 or pos_pct >= 0.70:
            return 'RANGE_REVERSAL'
        return None  # CHOP 区间中部拒单
    else:
        # 过渡态/冷却期：只允许极值位置
        if pos_pct <= 0.20:
            return 'RANGE_REVERSAL'
        elif pos_pct >= 0.80:
            return 'RANGE_REVERSAL'
    return None


def check_thresholds(trade_type: str, dir_score: float, loc_score: float,
                     trig_score: float, exec_score: float) -> Optional[str]:
    """检查四维门槛。返回 None=通过，否则返回拒因"""
    th = THRESHOLDS.get(trade_type, THRESHOLDS['TREND_PULLBACK'])
    if dir_score < th['dir']:
        return f'REJECT_LOW_DIR dir={dir_score:.1f}<{th["dir"]}'
    if loc_score < th['loc']:
        return f'REJECT_LOW_LOC loc={loc_score:.1f}<{th["loc"]}'
    if trig_score < th['trig']:
        return f'REJECT_LOW_TRIG trig={trig_score:.1f}<{th["trig"]}'
    if exec_score < th['exec']:
        return f'REJECT_LOW_EXEC exec={exec_score:.1f}<{th["exec"]}'
    return None


def get_stop_rule(trade_type: str) -> dict:
    return STOP_RULES.get(trade_type, STOP_RULES['TREND_PULLBACK'])


def get_signal_ttl(trade_type: str) -> int:
    return SIGNAL_TTL.get(trade_type, 60)

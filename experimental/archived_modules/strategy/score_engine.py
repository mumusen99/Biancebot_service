"""四维评分引擎：direction / location / trigger / execution。"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def compute_direction_score(
    ema9: float, ema21: float, btc_bias: int,
    vol_ratio: float, pullback_bars: int, side: str,
) -> float:
    """方向分 (0-10)：趋势结构 + BTC/ETH + 成交量方向"""
    score = 5.0

    # EMA 趋势
    if side == 'LONG' and ema9 > ema21:
        score += 1.5
    elif side == 'SHORT' and ema9 < ema21:
        score += 1.5
    else:
        score -= 2.0

    # BTC 偏置
    if side == 'LONG' and btc_bias > 0:
        score += min(btc_bias * 0.3, 2.0)
    elif side == 'SHORT' and btc_bias < 0:
        score += min(-btc_bias * 0.3, 2.0)
    elif btc_bias > 5 and side == 'SHORT':
        score -= 1.0  # BTC强牛做空扣分
    elif btc_bias < -5 and side == 'LONG':
        score -= 1.0

    # 量比
    if vol_ratio > 1.5:
        score += 1.0
    elif vol_ratio < 0.5:
        score -= 0.5

    return max(0, min(10, score))


def compute_location_score(
    pos_pct: float, near_vwap: bool, near_ema20: bool,
    extreme_penalty: float, side: str,
) -> float:
    """位置分 (0-10)：位置百分位 + VWAP/EMA + 支撑"""
    score = 5.0

    if side == 'LONG':
        if 0.20 <= pos_pct <= 0.50:
            score += 3.0  # 最佳位置
        elif pos_pct <= 0.20:
            score += 1.5  # 低位但不一定好
        else:
            score -= 2.0
    else:
        if 0.50 <= pos_pct <= 0.80:
            score += 3.0
        elif pos_pct >= 0.80:
            score += 1.5
        else:
            score -= 2.0

    if near_ema20:
        score += 1.0
    if near_vwap:
        score += 0.5

    score -= extreme_penalty / 2  # 极值惩罚影响位置分
    return max(0, min(10, score))


def compute_trigger_score(
    pullback_bars: int, rsi: float, hl_count: int,
    momentum_exhausted: bool, side: str,
) -> float:
    """触发分 (0-10)：K线确认 + 回踩质量 + 量价关系"""
    score = 5.0

    if 3 <= pullback_bars <= 6:
        score += 2.0
    elif pullback_bars > 6:
        score += 1.0
    else:
        score -= 1.0

    if side == 'LONG' and 40 <= rsi <= 60:
        score += 1.0
    elif side == 'SHORT' and 45 <= rsi <= 65:
        score += 1.0

    if hl_count >= 2:
        score += 1.0

    if momentum_exhausted:
        score -= 1.5

    return max(0, min(10, score))


def compute_execution_score(
    spread_bps: float, bs_ratio: float, signal_age_s: float,
) -> float:
    """执行分 (0-10)：点差 + 订单流 + 时效"""
    score = 6.0

    if spread_bps < 3:
        score += 2.0
    elif spread_bps < 5:
        score += 1.0
    elif spread_bps > 10:
        score -= 3.0

    if bs_ratio > 1.25:
        score += 1.0
    elif bs_ratio < 0.75:
        score -= 1.0

    if signal_age_s < 10:
        score += 1.0
    elif signal_age_s > 60:
        score -= 2.0

    return max(0, min(10, score))


class ScoreEngine:
    """统一评分接口"""

    def score(self, symbol: str, side: str, regime: str, btc_bias: int,
              ema9: float, ema21: float, pos_pct: float, vol_ratio: float,
              pullback_bars: int, rsi: float, hl_count: int,
              momentum_exhausted: bool, extreme_penalty: float,
              near_vwap: bool, near_ema20: bool,
              spread_bps: float = 2.0, bs_ratio: float = 1.0,
              signal_age_s: float = 0) -> dict:
        return {
            'dir_score': compute_direction_score(
                ema9, ema21, btc_bias, vol_ratio, pullback_bars, side),
            'loc_score': compute_location_score(
                pos_pct, near_vwap, near_ema20, extreme_penalty, side),
            'trig_score': compute_trigger_score(
                pullback_bars, rsi, hl_count, momentum_exhausted, side),
            'exec_score': compute_execution_score(
                spread_bps, bs_ratio, signal_age_s),
        }

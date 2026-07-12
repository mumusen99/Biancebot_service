"""概率型行情识别器：输出6维 softmax 概率，替代二元标签."""
from __future__ import annotations
import math
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def softmax(raw: list[float]) -> list[float]:
    """稳定 softmax"""
    mx = max(raw)
    exp = [math.exp(r - mx) for r in raw]
    total = sum(exp)
    return [e / total for e in exp]


def detect_regime_probabilities(
    btc_env: dict,
    top_coins: list[dict],
    df_1m=None,
    df_5m=None,
) -> dict:
    """
    输入 BTC 环境 + Top币统计，输出 6 维概率。
    返回: {TREND_UP, TREND_DOWN, RANGE, TRANSITION, HIGH_VOLATILITY, LOW_ACTIVITY}
    """
    raw = {
        'TREND_UP': 1.0,
        'TREND_DOWN': 1.0,
        'RANGE': 1.0,
        'TRANSITION': 1.0,
        'HIGH_VOLATILITY': 1.0,
        'LOW_ACTIVITY': 1.0,
    }

    btc_regime = btc_env.get('regime', 'unknown')
    btc_bias = btc_env.get('bias', 0)
    btc_change_24h = btc_env.get('change_24h', 0)

    # ── 1. BTC 趋势结构 (max ±4.0) ──
    if btc_bias >= 6:
        raw['TREND_UP'] += 4.0
        raw['TREND_DOWN'] -= 2.0
    elif btc_bias >= 3:
        raw['TREND_UP'] += 2.0
    elif btc_bias <= -6:
        raw['TREND_DOWN'] += 4.0
        raw['TREND_UP'] -= 2.0
    elif btc_bias <= -3:
        raw['TREND_DOWN'] += 2.0
    else:
        raw['RANGE'] += 1.5
        raw['TRANSITION'] += 1.0

    # ── 2. BTC 24h 变化 ──
    if abs(btc_change_24h) < 0.5:
        raw['RANGE'] += 1.0
        raw['LOW_ACTIVITY'] += 1.0
    elif abs(btc_change_24h) > 3:
        raw['HIGH_VOLATILITY'] += 2.0

    # ── 3. 市场宽度 ──
    if top_coins:
        n = len(top_coins)
        # 正向评分比例
        positive_ratio = sum(1 for c in top_coins if c.get('overall', 0) > 2) / max(n, 1)
        negative_ratio = sum(1 for c in top_coins if c.get('overall', 0) < -2) / max(n, 1)

        if positive_ratio > 0.6:
            raw['TREND_UP'] += 2.5
        elif positive_ratio > 0.4:
            raw['TREND_UP'] += 1.0
        elif negative_ratio > 0.6:
            raw['TREND_DOWN'] += 2.5
        elif negative_ratio > 0.4:
            raw['TREND_DOWN'] += 1.0

        # 分歧：正负都有
        if positive_ratio > 0.3 and negative_ratio > 0.3:
            raw['TRANSITION'] += 2.0

    # ── 4. 波动率 (从 ATR 估计) ──
    # 使用 Top3 币的平均相对强度变化作为波动代理
    if len(top_coins) >= 3:
        rs_values = [abs(c.get('relative_strength', 0)) for c in top_coins[:10]]
        avg_rs = sum(rs_values) / len(rs_values) if rs_values else 0
        if avg_rs > 4:
            raw['HIGH_VOLATILITY'] += 3.0
            raw['RANGE'] -= 1.0
            raw['TREND_UP'] -= 1.0
            raw['TREND_DOWN'] -= 1.0
        elif avg_rs < 1.0:
            raw['LOW_ACTIVITY'] += 2.0

    # ── 5. 震荡/趋势特征 (regime标签) ──
    if 'RISK_ON' in btc_regime:
        raw['TREND_UP'] += 2.0
        raw['RANGE'] -= 1.0
    elif 'RISK_OFF' in btc_regime:
        raw['TREND_DOWN'] += 2.0
        raw['RANGE'] -= 1.0
    elif 'CHOP' in btc_regime:
        raw['RANGE'] += 2.5
        raw['TREND_UP'] -= 1.0
        raw['TREND_DOWN'] -= 1.0
    elif 'DISTRIBUTION' in btc_regime:
        raw['TRANSITION'] += 3.0
        raw['TREND_UP'] -= 1.0
    elif 'COOLDOWN' in btc_regime or 'LOSS' in btc_regime:
        raw['TRANSITION'] += 2.0
        raw['LOW_ACTIVITY'] += 1.0

    # ── 6. 归一化 → softmax ──
    # 确保所有值 >= 0.1 避免极端
    for k in raw:
        raw[k] = max(raw[k], 0.1)

    labels = ['TREND_UP', 'TREND_DOWN', 'RANGE', 'TRANSITION', 'HIGH_VOLATILITY', 'LOW_ACTIVITY']
    vals = [raw[k] for k in labels]
    probs = softmax(vals)

    result = dict(zip(labels, [round(p, 4) for p in probs]))
    logger.debug(f'regime_probs: {result}')
    return result


def get_regime_confidence(probs: dict) -> float:
    """返回最大概率值，作为行情识别信心度 (0~1)"""
    return max(probs.values()) if probs else 0.5


def get_position_confidence_factor(probs: dict) -> float:
    """信心度 → 仓位因子"""
    conf = get_regime_confidence(probs)
    if conf >= 0.70:
        return 1.0
    elif conf >= 0.50:
        return 0.7
    elif conf >= 0.35:
        return 0.4
    return 0.2  # 极高不确定性

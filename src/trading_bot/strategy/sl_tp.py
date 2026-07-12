"""
SL/TP 计算器 (v4)
==================
按交易类型动态计算止损、止盈、R值、时间止损。

引用文档章节: 6-10, 20-21
"""
import math
from trading_bot.strategy.trade_router import TradeType, Direction

import logging
logger = logging.getLogger("v3")


def calc_sl_tp(
    symbol: str,
    df_1m,
    df_5m,
    trade_type: TradeType,
    direction: Direction,
    entry: float | None = None,
) -> dict:
    """
    计算 SL/TP/R/时间止损。

    返回:
      {entry, sl_hard, sl_soft, tp1, tp2, tp3, r, rr_to_tp1, time_stop_sec,
       partial_take_profit, max_slippage_price}
    """
    last1 = df_1m.iloc[-1]
    last5 = df_5m.iloc[-1]
    atr1 = float(last1.get("atr", 0.001))
    atr5 = float(last5.get("atr", 0.005))

    if entry is None:
        entry = float(last1["close"])

    # ── 按类型计算 ──
    if trade_type in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND):
        return _calc_momentum_sltp(symbol, df_1m, df_5m, direction, entry, atr1, atr5)
    elif trade_type == TradeType.FAILED_BREAKOUT:
        return _calc_fakeout_sltp(symbol, df_1m, df_5m, direction, entry, atr1, atr5)
    elif trade_type in (TradeType.PULLBACK_STANDARD, TradeType.BREAKOUT_RETEST):
        return _calc_standard_sltp(symbol, df_1m, df_5m, direction, entry, atr1, atr5, trade_type)
    else:
        # fallback: 宽止损
        sl = entry * 0.99 if direction == Direction.LONG else entry * 1.01
        r = abs(entry - sl)
        base = _build_sltp_result(symbol, direction, entry, sl, r, 180)
        base["reason_note"] = "fallback通用止损"
        return base


def _calc_momentum_sltp(symbol, df_1m, df_5m, direction, entry, atr1, atr5):
    """C/D: 动量类单，紧凑止损（8章/9章）"""
    last = df_1m.iloc[-1]
    if direction == Direction.LONG:
        sl1 = float(last["low"]) - atr1 * 0.3
        sl2 = entry - atr1 * 0.8
        sl = max(sl1, sl2)
        r = abs(entry - sl)
        # TP1=0.4R(50%)  TP2=0.8R(30%)  剩余20%移动
        tp1 = entry + r * 0.4
        tp2 = entry + r * 0.8
        partial = [(tp1, 0.5), (tp2, 0.8)]  # at tp1 close 50%, at tp2 close 80%
        sl_soft = tp1  # 达到TP1后止损移到entry附近
        max_slip = entry + r * 0.15
    else:
        sl1 = float(last["high"]) + atr1 * 0.3
        sl2 = entry + atr1 * 0.8
        sl = min(sl1, sl2)
        r = abs(sl - entry)
        tp1 = entry - r * 0.4
        tp2 = entry - r * 0.8
        partial = [(tp1, 0.5), (tp2, 0.8)]
        sl_soft = tp1
        max_slip = entry - r * 0.15

    result = _build_sltp_result(symbol, direction, entry, sl, r, 180)
    result.update({
        "tp1": tp1, "tp2": tp2, "tp3": None,
        "sl_soft": sl,
        "partial_take_profit": partial,
        "max_slippage_price": max_slip,
        "time_stop_sec": 180,
        "reason_note": "动量类紧凑止损",
    })
    return result


def _calc_fakeout_sltp(symbol, df_1m, df_5m, direction, entry, atr1, atr5):
    """E: 假突破反打（10章）"""
    last = df_1m.iloc[-1]
    if direction == Direction.LONG:
        sl = float(last["low"]) - atr1 * 0.4
        r = abs(entry - sl)
        tp1 = entry + r * 0.8
        tp2 = entry + r * 1.6
        partial = [(tp1, 0.5), (tp2, 1.0)]
        sl_soft = float(last["low"])
        max_slip = entry + r * 0.2
    else:
        sl = float(last["high"]) + atr1 * 0.4
        r = abs(sl - entry)
        tp1 = entry - r * 0.8
        tp2 = entry - r * 1.6
        partial = [(tp1, 0.5), (tp2, 1.0)]
        sl_soft = float(last["high"])
        max_slip = entry - r * 0.2

    result = _build_sltp_result(symbol, direction, entry, sl, r, 300)
    result.update({
        "tp1": tp1, "tp2": tp2, "tp3": None,
        "sl_soft": sl_soft,
        "partial_take_profit": partial,
        "max_slippage_price": max_slip,
        "time_stop_sec": 300,
        "reason_note": "假突破反打",
    })
    return result


def _calc_standard_sltp(symbol, df_1m, df_5m, direction, entry, atr1, atr5, trade_type):
    """A/B: 标准类单，5m结构止损（6章/7章）"""
    last5 = df_5m.iloc[-1]
    swing_low = float(last5.get("swing_low", entry * 0.99))
    swing_high = float(last5.get("swing_high", entry * 1.01))

    if direction == Direction.LONG:
        sl = min(swing_low - atr5 * 0.5, entry * 0.993)
        sl = max(sl, entry * 0.988)
        r = abs(entry - sl)
        tp1 = entry + r * 0.8
        tp2 = entry + r * 2.0
        tp3 = entry + r * 3.0 if trade_type == TradeType.BREAKOUT_RETEST else None
        partial = [(tp1, 0.4), (tp2, 0.8)]
        sl_soft = float(last5.get("swing_low", entry * 0.99))
        max_slip = entry + r * 0.2
    else:
        sl = max(swing_high + atr5 * 0.5, entry * 1.007)
        sl = min(sl, entry * 1.012)
        r = abs(sl - entry)
        tp1 = entry - r * 0.8
        tp2 = entry - r * 2.0
        tp3 = entry - r * 3.0 if trade_type == TradeType.BREAKOUT_RETEST else None
        partial = [(tp1, 0.4), (tp2, 0.8)]
        sl_soft = float(last5.get("swing_high", entry * 1.01))
        max_slip = entry - r * 0.2

    result = _build_sltp_result(symbol, direction, entry, sl, r, 300)
    result.update({
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "sl_soft": sl_soft,
        "partial_take_profit": partial,
        "max_slippage_price": max_slip,
        "time_stop_sec": 300,
        "reason_note": "标准类结构止损",
    })
    return result


def _build_sltp_result(symbol, direction, entry, sl_hard, r, time_stop_sec) -> dict:
    """构建基础SL/TP结果字典"""
    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl_hard": sl_hard,
        "sl_soft": sl_hard,
        "tp1": 0, "tp2": 0, "tp3": None,
        "r": r,
        "rr_to_tp1": 0,
        "rr_to_tp2": 0,
        "time_stop_sec": time_stop_sec,
        "partial_take_profit": [],
        "max_slippage_price": entry,
    }


def estimated_cost_pct(spread_pct: float = 0.0002) -> float:
    """粗略成本: 点差 + 手续费 + 滑点。引用17章。"""
    taker_fee_round_trip = 0.0008  # 0.08%
    estimated_slippage = max(spread_pct, 0.0002)
    return spread_pct + taker_fee_round_trip + estimated_slippage


def net_profit_ok(tp1_price: float, entry: float, direction: Direction, cost_pct: float, multiplier: int = 3) -> bool:
    """净收益过滤 (17章): 动量≥3倍成本, 标准≥4倍成本"""
    if direction == Direction.LONG:
        gross_pct = (tp1_price - entry) / entry
    else:
        gross_pct = (entry - tp1_price) / entry
    return gross_pct >= cost_pct * multiplier

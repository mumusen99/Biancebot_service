"""
过滤器 (v4)
============
开仓前检查：
  1. 位置过滤器 (12章): entry离支撑/EMA20距离
  2. BTC主导性过滤 (14章): BTC异常波动时降级
  3. BTC趋势反转检测: BTC连续趋势后首次破位
  4. 分级追涨规则 (11章): 轻/中/严重过热
  5. 盘口确认 (16章): 深度/主动买卖流
  6. 同板块联动 (15章): 收入版块判断

必须全部通过才允许生成交易计划。
"""
import logging
from trading_bot.strategy.trade_router import Direction, TradeType, MarketRegime

logger = logging.getLogger("v3")


def position_filter(
    sym: str,
    direction: Direction,
    entry: float,
    ema20_5m: float,
    atr5: float,
    swing_low: float,
    swing_high: float,
    r_target: float,
    trade_type: TradeType,
) -> tuple[bool, str]:
    """
    位置过滤器 (12章)。
    返回 (通过, 原因)
    """
    if direction == Direction.LONG:
        support_dist = entry - swing_low
        if support_dist > atr5 * 1.0:
            return False, f"距离支撑{support_dist/atr5:.1f}ATR > 1.0"

        space_ok = entry + r_target * 1.5
        if trade_type in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND):
            space_ok = entry + r_target * 0.8
        if trade_type not in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND):
            if (swing_high - entry) < r_target * 0.5:
                return False, f"上方空间不足({swing_high - entry:.4f} < 0.5R)"

        ema_dist = abs(entry - ema20_5m)
        if ema_dist > atr5 * 1.0:
            return False, f"距离EMA20 {ema_dist/atr5:.1f}ATR > 1.0"

    else:  # SHORT
        resist_dist = swing_high - entry
        if resist_dist > atr5 * 1.0:
            return False, f"距离压力{resist_dist/atr5:.1f}ATR > 1.0"

        if trade_type not in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND):
            if (entry - swing_low) < r_target * 0.5:
                return False, f"下方空间不足({entry - swing_low:.4f} < 0.5R)"

        ema_dist = abs(entry - ema20_5m)
        if ema_dist > atr5 * 1.0:
            return False, f"距离EMA20 {ema_dist/atr5:.1f}ATR > 1.0"

    return True, "通过"


def btc_dominance_filter(
    btc_1m_closes: list,
    btc_1m_highs: list,
    btc_1m_lows: list,
    btc_5m_closes: list,
    trade_type: TradeType,
    direction: Direction,
    atr_1m_recent: float,
    atr_1m_base: float,
) -> tuple[bool, str]:
    """
    BTC主导性过滤 (14章)。

    条件:
    - BTC近5根1m ATR > 过去20根1m ATR均值×1.8 → 禁止山寨动量快单
    - BTC近3根1m出现明显反向波动 → 山寨信号降级
    """
    if trade_type in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND):
        if atr_1m_base > 0 and atr_1m_recent > atr_1m_base * 1.8:
            return False, "BTC短线波动剧烈, 禁止山寨动量快单"

    if len(btc_1m_closes) >= 5:
        recent = btc_1m_closes[-3:]
        prev = btc_1m_closes[-5:-3] if len(btc_1m_closes) >= 5 else btc_1m_closes[-4:-2]
        avg_recent = sum(recent) / len(recent)
        avg_prev = sum(prev) / len(prev)
        btc_drift = (avg_recent - avg_prev) / avg_prev

        if direction == Direction.LONG and btc_drift < -0.005:
            return False, f"BTC反向{btc_drift*100:.2f}%, 降级山寨多单"
        if direction == Direction.SHORT and btc_drift > 0.010:
            return False, f"BTC反向{btc_drift*100:.2f}%, 降级山寨空单"

    return True, "通过"


def btc_trend_flip_filter(
    btc_5m_df,
    direction: Direction,
) -> tuple[bool, float, str]:
    """
    BTC趋势反转检测 (完善版):
    检测三种信号:
      1. 结构破位: 持续趋势后价格突破关键结构位
      2. EMA收敛: EMA9快速靠近EMA21但尚未交叉
      3. 趋势疲劳: 连续同向K线实体缩小或出现反向影线

    返回 (允许开仓, 风险系数, 说明)
    风险系数: 1.0=正常, 0.7=轻微降级, 0.5=减半, 0.3=极小仓
    """
    if btc_5m_df is None or len(btc_5m_df) < 40:
        return True, 1.0, "BTC数据不足"

    closes = [float(r["close"]) for _, r in btc_5m_df.iterrows()]
    from trading_bot.strategy.trade_router import ema as calc_ema
    price = closes[-1]
    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    ema_gap = abs(ema9 - ema21) / max(ema21, 0.01)  # EMA间距比例

    # 最近10根K线的ATR和结构位
    atr = 0
    trs = []
    for i in range(1, min(14, len(btc_5m_df))):
        h = float(btc_5m_df.iloc[-i]["high"])
        l = float(btc_5m_df.iloc[-i]["low"])
        pc = float(btc_5m_df.iloc[-i-1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / len(trs) if trs else 0
    
    # 10根K线的结构位（滞后2根防未来函数）
    lookback = 10
    swing_low = min(float(btc_5m_df.iloc[-i]["low"]) for i in range(3, 3+lookback))
    swing_high = max(float(btc_5m_df.iloc[-i]["high"]) for i in range(3, 3+lookback))
    
    # ─── 趋势状态分析 ───
    # 连续多头/空头计数
    bullish_streak = 0
    bearish_streak = 0
    for i in range(1, min(36, len(closes))):
        chunk = closes[:-i] if i < len(closes) else closes
        if len(chunk) < 21:
            break
        e9 = calc_ema(chunk, 9)
        e21 = calc_ema(chunk, 21)
        if e9 > e21:
            bullish_streak += 1
            bearish_streak = 0
        else:
            bearish_streak += 1
            bullish_streak = 0
    
    # EMA9斜率（最近5根）
    ema9_5ago = calc_ema(closes[:-6], 9) if len(closes) > 10 else calc_ema(closes, 9)
    ema9_slope = (ema9 - ema9_5ago) / max(ema9_5ago, 0.01) if ema9_5ago > 0 else 0
    
    # ─── 信号1: 持续多头后的结构破位 ───
    if direction == Direction.LONG and bullish_streak >= 10:
        # 价格跌破10根结构低点 → 多头破位
        if price < swing_low - atr * 0.3:
            factor = 0.5 if bullish_streak >= 24 else 0.7
            return True, factor, f"BTC多头{bullish_streak}bar后破swing_low, 风险{factor:.0f}折"
        
        # EMA收敛预警: EMA9快速靠近EMA21
        if ema_gap < 0.0005 and ema9_slope < 0:
            return True, 0.7, "BTC EMA9向EMA21收敛, 多单降级"
    
    # ─── 信号2: 持续空头后的结构破位 ───
    if direction == Direction.SHORT and bearish_streak >= 10:
        # 价格突破10根结构高点 → 空头破位
        if price > swing_high + atr * 0.3:
            factor = 0.5 if bearish_streak >= 24 else 0.7
            return True, factor, f"BTC空头{bearish_streak}bar后破swing_high, 风险{factor:.0f}折"
        
        # EMA收敛预警
        if ema_gap < 0.0005 and ema9_slope > 0:
            return True, 0.7, "BTC EMA9向EMA21收敛, 空单降级"
    
    # ─── 信号3: 趋势疲劳（价格与EMA9/21乖离过大） ───
    if direction == Direction.LONG and bullish_streak >= 30:
        close_vs_ema9 = (price - ema9) / ema9
        if close_vs_ema9 > 0.01:  # 价格远离EMA9超过1%
            return True, 0.7, "BTC连续上涨后乖离过大, 多单降级"
    
    if direction == Direction.SHORT and bearish_streak >= 30:
        close_vs_ema9 = (ema9 - price) / ema9
        if close_vs_ema9 > 0.01:
            return True, 0.7, "BTC连续下跌后乖离过大, 空单降级"

    return True, 1.0, "BTC趋势正常"


def overheat_filter(
    df_1m,
    atr5: float,
    direction: Direction,
    trade_type: TradeType,
) -> tuple[bool, float, str]:
    """
    分级追涨过滤 (11章)。
    返回 (通过, 风险系数, 原因)
    风险系数: 1.0=正常, 0.5=减半, 0.3=极小仓, 0=禁止
    """
    if len(df_1m) < 6:
        return True, 1.0, "数据不足不过热"

    try:
        gains = [
            float(df_1m.iloc[-i]["close"]) - float(df_1m.iloc[-i]["open"])
            for i in range(1, 6)
        ]
        total_move = abs(sum(gains))

        if total_move > atr5 * 3.0:
            if direction == Direction.LONG:
                return False, 0.0, f"严重过热{total_move/atr5:.1f}ATR, 禁止追多"
            return False, 0.0, f"严重过热{total_move/atr5:.1f}ATR, 禁止追空"

        elif total_move > atr5 * 2.0:
            if trade_type not in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND):
                return False, 0.0, f"中度过热{total_move/atr5:.1f}ATR, 仅允许动量快单"
            return True, 0.3, f"中度过热, 风险降至0.3"

        elif total_move > atr5 * 1.5:
            if trade_type not in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND):
                return False, 0.0, f"轻度过热{total_move/atr5:.1f}ATR, 仅允许动量快单"
            return True, 0.5, f"轻度过热, 风险降至0.5"

    except Exception:
        pass

    return True, 1.0, "不过热"


def sector_confirm_filter(
    sym: str,
    sector_map: dict,
    sector_heat: dict,
    change24h: float,
) -> tuple[bool, float]:
    """
    同板块联动确认 (15章)。
    返回 (通过, 风险系数)
    """
    sector = sector_map.get(sym, "other")
    sector_strength = sector_heat.get(sector, 0)

    if abs(sector_strength) < 0.2:
        if change24h > 0:
            return True, 0.5
    return True, 1.0


# ═══════════════════════════════════════════════════════════
#  新增过滤器 (趋势/震荡双引擎)
# ═══════════════════════════════════════════════════════════

def vwap_deviation_filter(
    vwap_data: dict,
    direction: str,
    trade_type: str,
    market_regime: str,
) -> tuple[bool, str]:
    """
    VWAP偏离过滤 (8章)。
    返回 (通过, 原因)
    """
    dev = vwap_data.get("vwap_deviation_atr", 0)

    if direction == "LONG":
        if dev > 1.8:
            return False, f"VWAP偏离{dev:.1f}ATR > 1.8, 禁止追多"
        if dev > 1.2:
            return False, f"VWAP偏离{dev:.1f}ATR > 1.2, 禁止标准多单"
    else:
        if dev < -1.8:
            return False, f"VWAP偏离{abs(dev):.1f}ATR < -1.8, 禁止追空"
        if dev < -1.2:
            return False, f"VWAP偏离{abs(dev):.1f}ATR < -1.2, 禁止标准空单"

    return True, "通过"


def position_percentile_filter(
    pct_data: dict,
    direction: str,
    trade_type: str,
) -> tuple[bool, int]:
    """
    位置百分位过滤 (4.1, 4.2) + 极值惩罚 (5章)。
    返回 (通过, 扣分)
    """
    pct = pct_data.get("pct_5m", 0.5)
    from trading_bot.strategy.position_utils import extreme_position_penalty
    penalty = extreme_position_penalty(pct, direction)

    if direction == "LONG":
        if pct > 0.90:
            return False, 0
        if pct > 0.85:
            return False, 0
        if pct > 0.75:
            # 允许动量快单
            if trade_type not in ("momentum_scalp", "momentum_second_entry"):
                return False, 0
    else:
        if pct < 0.10:
            return False, 0
        if pct < 0.15:
            return False, 0
        if pct < 0.25:
            if trade_type not in ("momentum_scalp", "momentum_second_entry"):
                return False, 0

    return True, penalty


def momentum_exhaustion_filter(
    df_1m,
    direction: str,
) -> tuple[bool, str]:
    """
    动量衰竭过滤 (9章)。
    """
    from trading_bot.strategy.position_utils import calc_momentum_exhaustion
    exhausted, reason = calc_momentum_exhaustion(df_1m, direction)
    if exhausted:
        return False, reason
    return True, ""


def range_middle_filter(
    pct_data: dict,
    market_regime: str,
) -> tuple[bool, str]:
    """
    区间中部禁止开仓 (11章)。
    震荡状态下，percentile 0.35~0.65禁止开新仓。
    """
    if market_regime != "CHOP":
        return True, ""

    pct = pct_data.get("pct_5m", 0.5)
    if 0.35 < pct < 0.65:
        return False, f"区间中部(pct={pct:.2f}), 禁止开仓"
    return True, ""

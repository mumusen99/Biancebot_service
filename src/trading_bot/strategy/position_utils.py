"""
位置工具集 (v4)
=============
引用: 超短线策略补充 — 趋势/震荡双引擎与极值位置过滤

功能：
  1. 位置百分位: 价格在局部滚动区间中的位置 (4章)
  2. VWAP计算与偏离检测 (8章)  
  3. 动量衰竭检测 (9章)
  4. 滚动均值/标准差 (Z-score) (10章)
"""
import logging
logger = logging.getLogger("v4")


def calc_position_percentile(closes: list, highs: list, lows: list,
                             lookback_1m: int = 45, lookback_5m: int = 25) -> dict:
    """
    价格位置百分位 (4章)。
    返回 {pct_1m, pct_5m, rolling_high_1m, rolling_low_1m, ...}
    """
    result = {"pct_1m": 0.5, "pct_5m": 0.5}
    if not closes:
        return result

    # 1m 窗口
    if len(closes) >= lookback_1m:
        c1 = closes[-lookback_1m:]
        h1 = highs[-lookback_1m:] if len(highs) >= lookback_1m else highs
        l1 = lows[-lookback_1m:] if len(lows) >= lookback_1m else lows
        rh1 = max(h1)
        rl1 = min(l1)
        rng = rh1 - rl1
        result["pct_1m"] = round((c1[-1] - rl1) / rng, 4) if rng > 0 else 0.5
        result["rolling_high_1m"] = rh1
        result["rolling_low_1m"] = rl1

    # 5m 窗口
    if len(closes) >= lookback_5m:
        c5 = closes[-lookback_5m:]
        h5 = highs[-lookback_5m:] if len(highs) >= lookback_5m else highs
        l5 = lows[-lookback_5m:] if len(lows) >= lookback_5m else lows
        rh5 = max(h5)
        rl5 = min(l5)
        rng = rh5 - rl5
        result["pct_5m"] = round((c5[-1] - rl5) / rng, 4) if rng > 0 else 0.5
        result["rolling_high_5m"] = rh5
        result["rolling_low_5m"] = rl5
        result["range_mid_5m"] = (rh5 + rl5) / 2

    return result


def calc_vwap(df_1m) -> dict:
    """
    计算VWAP及偏离度 (8章)。
    返回 {vwap, vwap_deviation_atr, price_vs_vwap}
    """
    result = {"vwap": 0, "vwap_deviation_atr": 0, "price_vs_vwap": "above"}
    if df_1m is None or len(df_1m) < 20:
        return result

    try:
        total_pv = 0.0
        total_vol = 0.0
        for i in range(max(0, len(df_1m) - 20), len(df_1m)):
            r = df_1m.iloc[i]
            high = float(r["high"])
            low = float(r["low"])
            close = float(r["close"])
            vol = float(r["volume"])
            typical = (high + low + close) / 3
            total_pv += typical * vol
            total_vol += vol

        if total_vol <= 0:
            return result

        vwap = total_pv / total_vol
        price = float(df_1m.iloc[-1]["close"])
        atr = float(df_1m.iloc[-1].get("atr", price * 0.002))
        deviation = (price - vwap) / max(atr, 0.001)

        result["vwap"] = vwap
        result["vwap_deviation_atr"] = deviation
        result["price_vs_vwap"] = "above" if price > vwap else "below"
    except Exception:
        pass

    return result


def calc_momentum_exhaustion(df_1m, direction: str) -> tuple[bool, str]:
    """
    动量衰竭检测 (9章)。
    返回 (是否衰竭, 原因)
    """
    if df_1m is None or len(df_1m) < 10:
        return False, ""

    try:
        closes = [float(r["close"]) for _, r in df_1m.iterrows()]
        highs = [float(r["high"]) for _, r in df_1m.iterrows()]
        lows = [float(r["low"]) for _, r in df_1m.iterrows()]
        volumes = [float(r["volume"]) for _, r in df_1m.iterrows()]

        last5 = closes[-5:]
        prev5 = closes[-10:-5]
        vol_last5 = sum(volumes[-5:])
        vol_prev5 = sum(volumes[-10:-5])

        # 成交量创新高但价格推进放缓
        vol_surge = vol_last5 > vol_prev5 * 1.5 if vol_prev5 > 0 else False

        if direction == "LONG":
            progress = last5[-1] - last5[0]
            prev_progress = prev5[-1] - prev5[0]
            # 实体缩小
            bodies = [abs(float(df_1m.iloc[-i]["close"]) - float(df_1m.iloc[-i]["open"])) for i in range(1, 4)]
            shrinking = len(bodies) >= 3 and bodies[0] < bodies[1] * 0.7 and bodies[1] < bodies[2] * 0.7
            # 上影线比例
            upper_wicks = [(highs[-i] - max(closes[-i], float(df_1m.iloc[-i]["open"]))) / max(highs[-i] - lows[-i], 0.001) for i in range(1, 4)]
            long_upper = any(w > 0.6 for w in upper_wicks)
            # 量增价不涨
            vol_up_no_progress = vol_surge and abs(progress) < abs(prev_progress) * 0.3 if abs(prev_progress) > 0 else False

            signals = sum([shrinking, long_upper, vol_up_no_progress])
            if signals >= 2:
                reasons = []
                if shrinking: reasons.append("实体缩小")
                if long_upper: reasons.append("长上影")
                if vol_up_no_progress: reasons.append("量增价滞")
                return True, "多单衰竭: " + ", ".join(reasons)

        else:  # SHORT
            progress = last5[0] - last5[-1]
            prev_progress = prev5[0] - prev5[-1]
            bodies = [abs(float(df_1m.iloc[-i]["close"]) - float(df_1m.iloc[-i]["open"])) for i in range(1, 4)]
            shrinking = len(bodies) >= 3 and bodies[0] < bodies[1] * 0.7 and bodies[1] < bodies[2] * 0.7
            lower_wicks = [(min(closes[-i], float(df_1m.iloc[-i]["open"])) - lows[-i]) / max(highs[-i] - lows[-i], 0.001) for i in range(1, 4)]
            long_lower = any(w > 0.6 for w in lower_wicks)
            vol_down_no_progress = vol_surge and abs(progress) < abs(prev_progress) * 0.3 if abs(prev_progress) > 0 else False

            signals = sum([shrinking, long_lower, vol_down_no_progress])
            if signals >= 2:
                reasons = []
                if shrinking: reasons.append("实体缩小")
                if long_lower: reasons.append("长下影")
                if vol_down_no_progress: reasons.append("量增价滞")
                return True, "空单衰竭: " + ", ".join(reasons)

    except Exception:
        pass

    return False, ""


def calc_zscore(closes: list, period: int = 20) -> float:
    """Z-score (10章)"""
    if not closes or len(closes) < period:
        return 0
    recent = closes[-period:]
    mean = sum(recent) / period
    var = sum((x - mean) ** 2 for x in recent) / period
    std = var ** 0.5 if var > 0 else 0.001
    return (recent[-1] - mean) / std


def extreme_position_penalty(pct: float, side: str) -> int:
    """
    极值位置惩罚分 (5章)。
    返回扣分数值。
    """
    if side == "LONG":
        if pct > 0.90: return -35
        if pct > 0.80: return -20
        if pct > 0.70: return -10
    else:  # SHORT
        if pct < 0.10: return -35
        if pct < 0.20: return -20
        if pct < 0.30: return -10
    return 0

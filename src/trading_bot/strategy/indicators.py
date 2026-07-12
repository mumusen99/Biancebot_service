"""
技术指标计算模块
----------------
基于 pandas 计算常用技术指标。
"""
import pandas as pd
import numpy as np
import logging

from trading_bot.core.settings import (
    EMA_FAST, EMA_SLOW,
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    BOLL_PERIOD, BOLL_STD,
)

logger = logging.getLogger("indicators")


def add_ema(df: pd.DataFrame, period: int = EMA_FAST) -> pd.DataFrame:
    """添加 EMA"""
    df[f"ema_{period}"] = df["close"].ewm(span=period, adjust=False).mean()
    return df


def add_sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """添加 SMA"""
    df[f"sma_{period}"] = df["close"].rolling(window=period).mean()
    return df


def add_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> pd.DataFrame:
    """添加 RSI"""
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    # RSI 信号
    df["rsi_signal"] = "neutral"
    df.loc[df["rsi"] < RSI_OVERSOLD, "rsi_signal"] = "oversold"
    df.loc[df["rsi"] > RSI_OVERBOUGHT, "rsi_signal"] = "overbought"
    return df


def add_macd(df: pd.DataFrame, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL) -> pd.DataFrame:
    """添加 MACD"""
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_histogram"] = df["macd"] - df["macd_signal"]
    return df


def add_bollinger(df: pd.DataFrame, period=BOLL_PERIOD, std=BOLL_STD) -> pd.DataFrame:
    """添加布林带"""
    df["boll_mid"] = df["close"].rolling(window=period).mean()
    rolling_std = df["close"].rolling(window=period).std()
    df["boll_upper"] = df["boll_mid"] + std * rolling_std
    df["boll_lower"] = df["boll_mid"] - std * rolling_std
    df["boll_width"] = (df["boll_upper"] - df["boll_lower"]) / df["boll_mid"]
    return df


def add_volume_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """添加成交量指标"""
    df["volume_sma"] = df["volume"].rolling(window=20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_sma"].replace(0, np.nan)
    df["volume_surge"] = df["volume_ratio"] > 1.5
    return df


def add_support_resistance(df: pd.DataFrame, lookback: int = 50) -> pd.DataFrame:
    """简易支撑阻力位 (基于最近 N 根 K 线的高低点)"""
    recent = df.tail(lookback)
    df["support"] = recent["low"].min()
    df["resistance"] = recent["high"].max()
    return df


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """计算所有指标"""
    df = df.copy()
    df = add_ema(df, EMA_FAST)
    df = add_ema(df, EMA_SLOW)
    df = add_sma(df)
    df = add_rsi(df)
    df = add_macd(df)
    df = add_bollinger(df)
    df = add_volume_indicators(df)
    df = add_support_resistance(df)
    return df


def generate_technical_signals(df: pd.DataFrame) -> dict:
    """
    基于最新 K 线数据生成多空信号强度评分。
    返回: {"long_score": 0-10, "short_score": 0-10, "signals": [...]}
    """
    if df.empty or len(df) < 50:
        return {"long_score": 5, "short_score": 5, "signals": ["数据不足，信号中性"]}

    df = compute_all(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    signals = []
    long_score = 0
    short_score = 0

    # ─── 1. EMA 交叉 ──────────────────────────────────
    ema_f_key = f"ema_{EMA_FAST}"
    ema_s_key = f"ema_{EMA_SLOW}"

    if prev[ema_f_key] <= prev[ema_s_key] and last[ema_f_key] > last[ema_s_key]:
        long_score += 2
        signals.append(f"✅ EMA{EMA_FAST} 上穿 EMA{EMA_SLOW} (金叉)")

    if prev[ema_f_key] >= prev[ema_s_key] and last[ema_f_key] < last[ema_s_key]:
        short_score += 2
        signals.append(f"🔴 EMA{EMA_FAST} 下穿 EMA{EMA_SLOW} (死叉)")

    # 价格相对 EMA 位置
    if last["close"] > last[ema_f_key] > last[ema_s_key]:
        long_score += 1
        signals.append("📈 价格在EMA上方，多头排列")
    elif last["close"] < last[ema_f_key] < last[ema_s_key]:
        short_score += 1
        signals.append("📉 价格在EMA下方，空头排列")

    # ─── 2. RSI ───────────────────────────────────────
    if last["rsi"] < RSI_OVERSOLD:
        long_score += 2
        signals.append(f"💪 RSI={last['rsi']:.1f} 超卖区域")
    elif last["rsi"] > RSI_OVERBOUGHT:
        short_score += 2
        signals.append(f"⚠️ RSI={last['rsi']:.1f} 超买区域")
    elif 40 <= last["rsi"] <= 60:
        signals.append(f"➖ RSI={last['rsi']:.1f} 中性区域")

    # RSI 背离
    if not df.empty and len(df) > RSI_PERIOD + 5:
        rsi_low = df["rsi"].tail(RSI_PERIOD + 5).min()
        price_low = df["close"].tail(RSI_PERIOD + 5).min()
        if last["rsi"] > rsi_low * 1.1 and last["close"] <= price_low * 1.01:
            long_score += 2
            signals.append("🔎 RSI 底背离 (价格新低但RSI走高)")

    # ─── 3. MACD ──────────────────────────────────────
    if prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]:
        long_score += 2
        signals.append("📊 MACD 金叉")
    if prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]:
        short_score += 2
        signals.append("📊 MACD 死叉")

    if last["macd_histogram"] > 0 and prev["macd_histogram"] <= 0:
        long_score += 1
        signals.append("📊 多头动能启动 (MACD柱翻正)")
    elif last["macd_histogram"] < 0 and prev["macd_histogram"] >= 0:
        short_score += 1
        signals.append("📊 空头动能启动 (MACD柱翻负)")

    # ─── 4. 布林带 ────────────────────────────────────
    boll_range = last["boll_upper"] - last["boll_lower"]
    mid = last["boll_mid"]

    if last["close"] <= last["boll_lower"] * 1.005:
        long_score += 1.5
        signals.append("📉 价格触及布林下轨，超卖")
    elif last["close"] >= last["boll_upper"] * 0.995:
        short_score += 1.5
        signals.append("📈 价格触及布林上轨，超买")

    if last["boll_width"] < 0.05:
        signals.append("🔇 布林带收窄，可能变盘")
        long_score += 1
        short_score += 1  # 变盘方向不确定，双方都加分等待确认

    # ─── 5. 成交量 ────────────────────────────────────
    if last["volume_surge"]:
        if last["close"] > prev["close"]:
            long_score += 1
            signals.append(f"📢 放量上涨 (成交量比={last['volume_ratio']:.1f}x)")
        else:
            short_score += 1
            signals.append(f"📢 放量下跌 (成交量比={last['volume_ratio']:.1f}x)")

    # 缩量
    if last["volume_ratio"] < 0.5:
        signals.append("🔇 缩量，市场观望")

    # ─── 6. 趋势方向判断 ──────────────────────────────
    close_5ago = df["close"].iloc[-5]
    if last["close"] > close_5ago * 1.02:
        long_score += 1
    elif last["close"] < close_5ago * 0.98:
        short_score += 1

    # ─── 归一化到 10 分制 ───────────────────────────
    long_score = min(10, max(0, long_score))
    short_score = min(10, max(0, short_score))

    return {
        "long_score": long_score,
        "short_score": short_score,
        "signals": signals,
        "trend": "LONG" if long_score > short_score + 2 else "SHORT" if short_score > long_score + 2 else "NEUTRAL",
        "last_price": float(last["close"]),
        "support": float(df["support"].iloc[-1]),
        "resistance": float(df["resistance"].iloc[-1]),
    }


def compute_scalp_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """舔头皮专用：一站式计算所有技术指标。纯函数，无网络/无状态。"""
    df = df.copy()
    close = df['close']
    # EMA
    df['ema9'] = close.ewm(span=9, adjust=False).mean()
    df['ema21'] = close.ewm(span=21, adjust=False).mean()
    df['ema20'] = close.ewm(span=20, adjust=False).mean()
    # RSI 14
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    rs = gain.rolling(14).mean() / loss.rolling(14).mean().replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    # 布林带
    df['sma20'] = close.rolling(20).mean()
    df['bb_std'] = close.rolling(20).std()
    df['bb_upper'] = df['sma20'] + 2 * df['bb_std']
    df['bb_lower'] = df['sma20'] - 2 * df['bb_std']
    # 成交量
    df['vol_avg'] = df['volume'].rolling(10).mean()
    df['volume_ma20'] = df['volume'].rolling(20).mean()
    df['volume_ratio'] = df['volume'] / df['volume_ma20'].replace(0, np.nan)
    # ATR(14)
    high, low = df['high'], df['low']
    prev_close = close.shift(1)
    tr = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    # VWAP
    df['vwap'] = (close * df['volume']).cumsum() / df['volume'].cumsum()
    # 摆动高低点
    df['swing_low'] = low.rolling(10, min_periods=10).min()
    df['swing_high'] = high.rolling(10, min_periods=10).max()
    # MACD (12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_histogram'] = df['macd'] - df['macd_signal']
    return df

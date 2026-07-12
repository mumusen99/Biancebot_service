"""
回测：分层策略在 ETHUSDT 近3年表现
====================================
策略规则（scalper 版）:
  第1层 BTC 环境 → 决定方向偏好
  第2层 RSI+布林带 → ETH 入场信号
  止盈+10%保证金(2%价格) / 止损-5%保证金(1%价格)
"""
import json, time, logging, sys
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import requests as req

from config import PROXY

prox = {"http": PROXY, "https": PROXY}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bt] %(message)s")
logger = logging.getLogger("backtest")

# ─── 策略参数（与 scalper 一致） ──────────────────
LEVERAGE = 5
AMOUNT_USDT = 10
SL_MARGIN_PCT = 5.0   # 止损保证金%
TP_MARGIN_PCT = 10.0  # 止盈保证金%
SL_PRICE_PCT = SL_MARGIN_PCT / LEVERAGE    # 1%价格
TP_PRICE_PCT = TP_MARGIN_PCT / LEVERAGE    # 2%价格

# BTC 环境阈值
BTC_BULL_RSI_MIN = 55
BTC_BEAR_RSI_MAX = 45
RSI_LONG_MAX = 25    # RSI<25 超卖入场
RSI_SHORT_MIN = 75   # RSI>75 超买入场
TIMEFRAME = "1h"
LOOKBACK_YEARS = 3


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算 RSI + 布林带 + EMA"""
    close = df["close"]
    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    # 布林带
    df["sma20"] = close.rolling(20).mean()
    df["bb_std"] = close.rolling(20).std()
    df["bb_upper"] = df["sma20"] + 2 * df["bb_std"]
    df["bb_lower"] = df["sma20"] - 2 * df["bb_std"]
    # EMA
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    return df


def get_btc_regime(btc_df: pd.DataFrame, idx: int) -> dict:
    """取 idx 处 BTC 环境"""
    if idx < 50:
        return {"regime": "unknown", "direction": "both"}
    row = btc_df.iloc[idx]
    close = row["close"]
    sma20 = btc_df["close"].iloc[:idx+1].rolling(20).mean().iloc[-1]
    sma50 = btc_df["close"].iloc[:idx+1].rolling(50).mean().iloc[-1]
    rsi = row.get("rsi", 50)
    
    above_sma20 = close > sma20
    above_sma50 = close > sma50
    bullish_ma = sma20 > sma50
    
    if above_sma20 and above_sma50 and bullish_ma and rsi > BTC_BULL_RSI_MIN:
        return {"regime": "bull", "direction": "long"}
    elif not above_sma20 and not above_sma50 and rsi < BTC_BEAR_RSI_MAX:
        return {"regime": "bear", "direction": "short"}
    else:
        return {"regime": "range", "direction": "both"}


def compute_kol_columns(eth_df: pd.DataFrame) -> pd.DataFrame:
    """预计算所有KOL相关列（矢量化）"""
    df = eth_df.copy()
    close = df["close"]
    
    # EMA
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    
    # 价格相对EMA
    df["above_ema50"] = close > df["ema50"]
    df["ema_bullish"] = df["ema20"] > df["ema50"]
    
    # 近5根K线累计涨幅
    df["recent5_pct"] = close.pct_change(4) * 100
    
    # 连续涨跌计数
    df["up_count"] = (close.diff() > 0).rolling(5).sum()
    df["down_count"] = (close.diff() < 0).rolling(5).sum()
    
    return df


def kol_sentiment_proxy(row: pd.Series) -> dict:
    """
    基于预计算列判断KOL情绪（单行，矢量化友好）
    """
    rsi = row.get("rsi", 50)
    above_ema50 = row.get("above_ema50", False)
    ema_bullish = row.get("ema_bullish", False)
    recent_pct = row.get("recent5_pct", 0)
    up_count = row.get("up_count", 0)
    down_count = row.get("down_count", 0)
    
    # 强信号
    if above_ema50 and ema_bullish and rsi > 55 and recent_pct > 1 and up_count >= 4:
        return {"sentiment": "bullish", "warning": "KOL偏多,避免做空"}
    if not above_ema50 and not ema_bullish and rsi < 45 and recent_pct < -1 and down_count >= 4:
        return {"sentiment": "bearish", "warning": "KOL偏空,避免做多"}
    
    # 弱信号
    if above_ema50 and rsi > 55 and up_count >= 3 and recent_pct > 0.5:
        return {"sentiment": "mild_bullish", "warning": "KOL偏多,做空谨慎"}
    if not above_ema50 and rsi < 45 and down_count >= 3 and recent_pct < -0.5:
        return {"sentiment": "mild_bearish", "warning": "KOL偏空,做多谨慎"}
    
    return {"sentiment": "neutral", "warning": ""}


def fetch_klines_local(symbol: str, years: int, tf: str) -> pd.DataFrame:
    """从文件缓存或API获取K线"""
    cache_file = Path(f"cache_{symbol}_{tf}_{years}y.csv")
    if cache_file.exists():
        logger.info(f"📂 读取缓存 {cache_file}")
        df = pd.read_csv(cache_file, parse_dates=["time"])
        return df
    
    logger.info(f"📥 下载 {symbol} {tf} {years}年数据...")
    limit = 1000
    end_time = int(datetime.now().timestamp() * 1000)
    bars = []
    
    # 计算需要多少根K线
    tf_ms = {"1h": 3600000, "4h": 14400000}
    ms = tf_ms.get(tf, 3600000)
    total_needed = int(years * 365 * 24 * 3600000 / ms)
    
    while len(bars) < total_needed:
        url = (f"https://fapi.binance.com/fapi/v1/klines"
               f"?symbol={symbol}&interval={tf}&limit={limit}"
               f"&endTime={end_time}")
        resp = req.get(url, timeout=30, proxies=prox)
        data = resp.json()
        if not data:
            break
        for k in data:
            bars.append({
                "time": datetime.fromtimestamp(k[0] / 1000),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),
            })
        end_time = data[0][0] - 1
        logger.info(f"  📊 {len(bars)}/{total_needed} 根K线 (最新: {bars[-1]['time']})")
        time.sleep(0.1)
        if len(data) < limit:
            break
    
    df = pd.DataFrame(bars)
    df = df.sort_values("time").reset_index(drop=True)
    df.to_csv(cache_file, index=False)
    logger.info(f"💾 缓存已保存: {cache_file} ({len(df)}行)")
    return df


def backtest():
    logger.info(f"🚀 开始回测 ETHUSDT, 时间框架={TIMEFRAME}, 回看{LOOKBACK_YEARS}年")
    logger.info(f"策略: SL={SL_MARGIN_PCT}%保证金(-{SL_PRICE_PCT}%价格) "
                f"TP={TP_MARGIN_PCT}%保证金(+{TP_PRICE_PCT}%价格)")
    
    # 获取数据
    eth_df = fetch_klines_local("ETHUSDT", LOOKBACK_YEARS, TIMEFRAME)
    btc_df = fetch_klines_local("BTCUSDT", LOOKBACK_YEARS, TIMEFRAME)
    
    # 计算指标
    eth_df = compute_indicators(eth_df)
    btc_df = compute_indicators(btc_df)
    
    # 对齐时间轴（取交集）
    min_time = max(eth_df["time"].min(), btc_df["time"].min())
    eth_df = eth_df[eth_df["time"] >= min_time].reset_index(drop=True)
    btc_df = btc_df[btc_df["time"] >= min_time].reset_index(drop=True)
    
    logger.info(f"📊 ETH: {len(eth_df)}行, BTC: {len(btc_df)}行")
    
    # 将BTC的regime批量预计算（矢量化避免逐行slow rolling）
    logger.info("⏳ 预计算BTC环境...")
    btc_regimes_list = []
    for i in range(len(btc_df)):
        if i < 50:
            btc_regimes_list.append({"regime": "unknown", "direction": "both"})
        else:
            btc_regimes_list.append(get_btc_regime(btc_df, i))
    
    # 映射到ETH时间轴
    btc_times = btc_df["time"].values
    btc_regimes = []
    for i in range(len(eth_df)):
        t = eth_df.iloc[i]["time"]
        btc_idx = np.searchsorted(btc_times, t, side="right") - 1
        if btc_idx < 50:
            btc_regimes.append({"regime": "unknown", "direction": "both"})
        else:
            btc_regimes.append(btc_regimes_list[btc_idx])
    logger.info(f"✅ BTC环境预计算完成 ({len(btc_regimes)}个时间点)")
    
    # 预计算ETH KOL列
    logger.info("⏳ 预计算ETH KOL情绪...")
    eth_df = compute_kol_columns(eth_df)
    logger.info("✅ KOL预计算完成")
    
    # ─── 回测主循环 ───
    trades = []
    in_position = False
    entry_price = 0
    entry_side = ""
    entry_idx = 0
    entry_regime = ""
    
    for i in range(50, len(eth_df)):
        row = eth_df.iloc[i]
        close = row["close"]
        rsi = row["rsi"]
        bb_lower = row["bb_lower"]
        bb_upper = row["bb_upper"]
        prev_close = eth_df.iloc[i - 1]["close"]
        btc_r = btc_regimes[i]
        regime = btc_r["regime"]
        direction = btc_r["direction"]
        
        if in_position:
            # 检查是否触止盈止损
            if entry_side == "LONG":
                loss_pct = (close - entry_price) / entry_price * 100
                if loss_pct <= -SL_PRICE_PCT:
                    win = loss_pct * LEVERAGE  # 保证金盈亏%
                    trades[-1]["exit_reason"] = "SL"
                    trades[-1]["exit_price"] = close
                    trades[-1]["pnl_pct"] = round(win, 2)
                    trades[-1]["pnl_usdt"] = round(AMOUNT_USDT * win / 100, 2)
                    in_position = False
                elif loss_pct >= TP_PRICE_PCT:
                    win = loss_pct * LEVERAGE
                    trades[-1]["exit_reason"] = "TP"
                    trades[-1]["exit_price"] = close
                    trades[-1]["pnl_pct"] = round(win, 2)
                    trades[-1]["pnl_usdt"] = round(AMOUNT_USDT * win / 100, 2)
                    in_position = False
            else:  # SHORT
                loss_pct = (entry_price - close) / entry_price * 100
                if loss_pct <= -SL_PRICE_PCT:
                    win = loss_pct * LEVERAGE
                    trades[-1]["exit_reason"] = "SL"
                    trades[-1]["exit_price"] = close
                    trades[-1]["pnl_pct"] = round(win, 2)
                    trades[-1]["pnl_usdt"] = round(AMOUNT_USDT * win / 100, 2)
                    in_position = False
                elif loss_pct >= TP_PRICE_PCT:
                    win = loss_pct * LEVERAGE
                    trades[-1]["exit_reason"] = "TP"
                    trades[-1]["exit_price"] = close
                    trades[-1]["pnl_pct"] = round(win, 2)
                    trades[-1]["pnl_usdt"] = round(AMOUNT_USDT * win / 100, 2)
                    in_position = False
        
        # KOL 单边预警
        kol = kol_sentiment_proxy(row)
        kol_warning = kol["warning"]
        
        # 检查入场信号（KOL预警可覆盖BTC方向判断）
        if in_position:
            continue
        
        # KOL强预警时禁止逆势开单
        kol_block_long = kol["sentiment"] in ("bearish", "mild_bearish")
        kol_block_short = kol["sentiment"] in ("bullish", "mild_bullish")
        
        long_allowed = direction != "short" and not kol_block_long
        short_allowed = direction != "long" and not kol_block_short
        
        long_signal = (rsi < RSI_LONG_MAX and close <= bb_lower * 1.01
                       and close > prev_close and long_allowed)
        short_signal = (rsi > RSI_SHORT_MIN and close >= bb_upper * 0.99
                        and close < prev_close and short_allowed)
        
        if long_signal:
            in_position = True
            entry_price = close
            entry_side = "LONG"
            entry_idx = i
            entry_regime = regime
            trades.append({
                "entry_time": row["time"],
                "entry_price": close,
                "side": "LONG",
                "btc_regime": regime,
                "kol": kol["sentiment"],
                "kol_warning": kol_warning,
                "rsi": round(rsi, 1),
                "exit_price": None,
                "exit_reason": None,
                "pnl_pct": None,
                "pnl_usdt": None,
            })
        elif short_signal:
            in_position = True
            entry_price = close
            entry_side = "SHORT"
            entry_idx = i
            entry_regime = regime
            trades.append({
                "entry_time": row["time"],
                "entry_price": close,
                "side": "SHORT",
                "btc_regime": regime,
                "kol": kol["sentiment"],
                "kol_warning": kol_warning,
                "rsi": round(rsi, 1),
                "exit_price": None,
                "exit_reason": None,
                "pnl_pct": None,
                "pnl_usdt": None,
            })
    
    # 还在持仓的按当前价平
    if in_position and trades:
        last_close = eth_df.iloc[-1]["close"]
        t = trades[-1]
        if t["side"] == "LONG":
            pnl = (last_close - t["entry_price"]) / t["entry_price"] * 100
        else:
            pnl = (t["entry_price"] - last_close) / t["entry_price"] * 100
        t["exit_price"] = last_close
        t["exit_reason"] = "OPEN"
        t["pnl_pct"] = round(pnl * LEVERAGE, 2)
        t["pnl_usdt"] = round(AMOUNT_USDT * pnl * LEVERAGE / 100, 2)
    
    # ─── 统计 ───
    total = len(trades)
    wins = [t for t in trades if t.get("pnl_usdt", 0) > 0]
    losses = [t for t in trades if t.get("pnl_usdt", 0) <= 0]
    win_rate = len(wins) / total * 100 if total else 0
    total_pnl = sum(t.get("pnl_usdt", 0) for t in trades)
    avg_win = sum(t.get("pnl_usdt", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.get("pnl_usdt", 0) for t in losses) / len(losses) if losses else 0
    
    # 按BTC环境统计
    by_regime = {}
    for r in ["bull", "bear", "range"]:
        rt = [t for t in trades if t.get("btc_regime") == r]
        rw = [t for t in rt if t.get("pnl_usdt", 0) > 0]
        by_regime[r] = {
            "trades": len(rt),
            "wins": len(rw),
            "win_rate": len(rw) / len(rt) * 100 if rt else 0,
            "pnl": round(sum(t.get("pnl_usdt", 0) for t in rt), 2),
        }
    
    # 按KOL情绪统计
    by_kol = {}
    for k in ["bullish", "bearish", "neutral", "mild_bullish", "mild_bearish"]:
        kt = [t for t in trades if t.get("kol") == k]
        kw = [t for t in kt if t.get("pnl_usdt", 0) > 0]
        if kt:
            by_kol[k] = {
                "trades": len(kt),
                "wins": len(kw),
                "win_rate": len(kw) / len(kt) * 100 if kt else 0,
                "pnl": round(sum(t.get("pnl_usdt", 0) for t in kt), 2),
            }
    
    # ─── 输出报告 ───
    print(f"\n{'='*60}")
    print(f"📊 分层策略+KOL预警回测 — ETHUSDT ({TIMEFRAME})")
    print(f"回测期间: {eth_df.iloc[50]['time'].date()} ~ {eth_df.iloc[-1]['time'].date()}")
    print(f"总交易数: {total}")
    print(f"胜率: {win_rate:.1f}% ({len(wins)}胜/{len(losses)}负)")
    print(f"总盈亏: {total_pnl:+.2f}U (每单{AMOUNT_USDT}U)")
    print(f"平均盈利: {avg_win:+.2f}U")
    print(f"平均亏损: {avg_loss:+.2f}U")
    print(f"盈亏比: {abs(avg_win / avg_loss):.2f}" if avg_loss != 0 else "盈亏比: ∞")
    print(f"{'='*60}")
    
    print(f"\n按BTC环境拆分:")
    for r in ["bull", "bear", "range"]:
        d = by_regime[r]
        if d["trades"]:
            print(f"  {r:<6}: {d['trades']}笔 胜率{d['win_rate']:.0f}% PnL{d['pnl']:+.2f}U")
    
    print(f"\n按KOL情绪拆分:")
    for k in ["bullish", "mild_bullish", "neutral", "mild_bearish", "bearish"]:
        d = by_kol.get(k)
        if d:
            print(f"  {k:<14}: {d['trades']}笔 胜率{d['win_rate']:.0f}% PnL{d['pnl']:+.2f}U")
    
    # 按年份
    print(f"\n按年份拆分:")
    for year in sorted(set(t["entry_time"].year for t in trades)):
        yt = [t for t in trades if t["entry_time"].year == year]
        yw = [t for t in yt if t.get("pnl_usdt", 0) > 0]
        yp = sum(t.get("pnl_usdt", 0) for t in yt)
        print(f"  {year}: {len(yt)}笔 胜率{len(yw)/len(yt)*100:.0f}% PnL{yp:+.2f}U")
    
    # Top5最佳/最差
    sorted_trades = sorted(trades, key=lambda t: t.get("pnl_usdt", 0))
    print(f"\n最佳3笔:")
    for t in sorted_trades[-3:]:
        print(f"  +{t['pnl_usdt']:+.2f}U {t['side']} @{t['entry_price']} "
              f"{t['entry_time'].strftime('%Y-%m-%d %H:%M')} BTC={t['btc_regime']}")
    print(f"\n最差3笔:")
    for t in sorted_trades[:3]:
        print(f"  {t['pnl_usdt']:+.2f}U {t['side']} @{t['entry_price']} "
              f"{t['entry_time'].strftime('%Y-%m-%d %H:%M')} BTC={t['btc_regime']}")
    
    # 月度汇总
    # KOL 拦截统计（模拟思路：统计被KOL拦掉的潜在亏损交易）
    kol_blocked = 0
    kol_blocked_loss = 0
    for i in range(50, len(eth_df)):
        row = eth_df.iloc[i]
        close = row["close"]
        rsi = row["rsi"]
        bb_lower = row["bb_lower"]
        bb_upper = row["bb_upper"]
        prev_close = eth_df.iloc[i - 1]["close"]
        
        kol = kol_sentiment_proxy(eth_df.iloc[i])
        btc_r = btc_regimes[i]
        direction = btc_r["direction"]
        
        kol_block_long = kol["sentiment"] in ("bearish", "mild_bearish")
        kol_block_short = kol["sentiment"] in ("bullish", "mild_bullish")
        
        long_signal = rsi < RSI_LONG_MAX and close <= bb_lower * 1.01 and close > prev_close
        short_signal = rsi > RSI_SHORT_MIN and close >= bb_upper * 0.99 and close < prev_close
        
        if long_signal and kol_block_long:
            kol_blocked += 1
            # 模拟：如果没被拦，这笔大概率会亏多少
            future_idx = min(i + 2, len(eth_df) - 1)
            future_price = eth_df.iloc[future_idx]["close"]
            simulated_loss = min(0, (future_price - close) / close * 100 * LEVERAGE)
            kol_blocked_loss += simulated_loss
    
    print(f"\n📊 KOL预警拦截统计:")
    print(f"  拦截潜在逆势开单: {kol_blocked}笔")
    print(f"  估算避免亏损: {kol_blocked_loss:+.2f}U")
    
    print(f"\n月度PnL热力图:")
    monthly = {}
    for t in trades:
        ym = t["entry_time"].strftime("%Y-%m")
        monthly.setdefault(ym, 0)
        monthly[ym] += t.get("pnl_usdt", 0)
    for ym in sorted(monthly, reverse=True)[:12]:
        pnl = monthly[ym]
        print(f"  {ym}: {pnl:+.2f}U {'🟢' if pnl > 0 else '🔴' if pnl < 0 else '⚪'}")


if __name__ == "__main__":
    backtest()

#!/usr/bin/env python3
"""
BTC熊机检查 - 每小时跑一次
检查BTCUSDT是否出现做空信号
"""
import sys, time, json, os
sys.path.insert(0, os.path.dirname(__file__))

from trader import _get_price
from kol_coin_sentiment import get_coin_sentiment

def log(msg):
    print(msg, flush=True)

def retry_call(fn, args=(), kwargs=None, max_retries=8, delay=5):
    for i in range(max_retries):
        try:
            return fn(*args, **(kwargs or {}))
        except Exception as e:
            log(f"⏳ 第{i+1}次失败: {type(e).__name__}")
            time.sleep(delay)
    raise Exception(f"重试{max_retries}次均失败: {fn.__name__}")

# ── 1. 获取价格 ──
log("📊 BTCUSDT 价格...")
price = retry_call(_get_price, ["BTCUSDT"])
log(f"   ${price:.2f}")

# ── 2. K线技术分析 ──
from data_fetcher import fetch_klines
from indicators import compute_all, generate_technical_signals

results = {}
for tf in ['1h', '4h', '1d']:
    df = retry_call(fetch_klines, kwargs={"symbol": "BTCUSDT", "timeframe": tf, "limit": 100})
    if df.empty:
        log(f"  {tf}: 无数据")
        continue
    df = compute_all(df)
    sig = generate_technical_signals(df)
    last = df.iloc[-1]
    
    rsi = last.get("rsi")
    macdh = last.get("macdh")
    ema21 = last.get("ema21")
    
    # 布林位置(列名: boll_upper,boll_mid,boll_lower)
    bb_pos = "n/a"
    bbu = last.get("boll_upper")
    bbl = last.get("boll_lower")
    if bbu is not None and bbl is not None:
        try:
            bbu_f, bbl_f = float(bbu), float(bbl)
            if abs(bbu_f - bbl_f) > 0.001:
                cp = float(last["close"])
                pct = (cp - bbl_f) / (bbu_f - bbl_f) * 100
                if cp >= bbu_f:
                    bb_pos = "上轨↑"
                elif cp <= bbl_f:
                    bb_pos = "下轨↓"
                else:
                    bb_pos = f"{pct:.0f}%"
        except:
            pass
    
    results[tf] = {
        "close": float(last["close"]),
        "rsi": float(rsi) if rsi is not None else None,
        "macdh": float(macdh) if macdh is not None else None,
        "ema21": float(ema21) if ema21 is not None else None,
        "bb_pos": bb_pos,
        "trend": sig.get("trend", "UNKNOWN"),
        "long_score": sig.get("long_score", 0),
        "short_score": sig.get("short_score", 0),
        "signals": sig.get("signals", []),
        "support": sig.get("support", 0),
        "resistance": sig.get("resistance", 0),
    }
    
    log(f"  {tf}: trend={results[tf]['trend']} RSI={results[tf]['rsi']} BB={bb_pos} L{results[tf]['long_score']}/S{results[tf]['short_score']}")

# ── 3. KOL情绪 ──
log("🌐 KOL情绪检索...")
try:
    sent = retry_call(get_coin_sentiment, [["BTCUSDT"]])
    kol = sent.get("BTCUSDT", {})
    kol_sent = kol.get("sentiment", "neutral")
    kol_bull = kol.get("bull_count", 0)
    kol_bear = kol.get("bear_count", 0)
    kol_score = kol.get("score", 0)
    log(f"   {kol_sent} (牛{kol_bull}/熊{kol_bear}, 分{kol_score})")
except Exception as e:
    log(f"   情绪失败: {e}")
    kol_sent, kol_bull, kol_bear, kol_score = "unknown", 0, 0, 0

# ── 4. 综合判断 ──
short_signals = []
long_signals = []

# 价格 vs EMA21
if "4h" in results and results["4h"]["ema21"]:
    if price < results["4h"]["ema21"]:
        short_signals.append("📉 价格在4h EMA下方")
    else:
        long_signals.append("📈 价格在4h EMA上方")

if "1h" in results and results["1h"]["rsi"]:
    r1 = results["1h"]["rsi"]
    if r1 > 70:
        short_signals.append(f"⚠️ 1h RSI={r1:.0f} 超买")
    elif r1 > 65:
        short_signals.append(f"⚠️ 1h RSI={r1:.0f} 接近超买")
    elif r1 < 30:
        long_signals.append(f"💪 1h RSI={r1:.0f} 超卖")

if "4h" in results:
    r4 = results["4h"]
    if r4.get("bb_pos", "").startswith("上轨"):
        short_signals.append("⚠️ 4h触及布林上轨")
    if r4.get("bb_pos", "").startswith("下轨"):
        long_signals.append("💪 4h触及布林下轨")
    if r4["macdh"] is not None:
        if r4["macdh"] < 0:
            short_signals.append("📉 4h MACD柱为负")
        else:
            long_signals.append("📈 4h MACD柱为正")

if kol_sent == "bearish":
    short_signals.append("🐻 KOL看空")
elif kol_sent == "bullish":
    long_signals.append("🐂 KOL看多")

short_score = len(short_signals)
long_score = len(long_signals)

# ── 5. 结论 ──
if short_score >= 3 and short_score > long_score:
    verdict = "✅ 做空机会!"
elif short_score == 2 and short_score > long_score:
    verdict = "⚠️ 有偏空信号，可关注"
elif short_score == 2 and short_score == long_score:
    verdict = "⚖️ 多空信号均衡"
elif short_score >= 4:
    verdict = "🔴 强烈做空信号!"
else:
    verdict = "👀 观望"

report = f"""📊 BTC熊机检查 @ {time.strftime('%H:%M')}

💰 ${price:.2f}

技术面:"""
for tf in ['1h', '4h', '1d']:
    r = results.get(tf)
    if r:
        report += f"\n  {tf} {r['trend']:8s} RSI{r['rsi']:5.1f} BB{r['bb_pos']:>6s}  L{r['long_score']}/S{r['short_score']}"

report += f"""

情绪: {'🐻看空' if kol_sent == 'bearish' else '🐂看多' if kol_sent == 'bullish' else '⚪中性'} (牛{kol_bull}/熊{kol_bear})
空头信号 ({short_score}):"""
for s in short_signals:
    report += f"\n  🔴 {s}"
if not short_signals:
    report += "\n  (无)"

report += f"\n多头信号 ({long_score}):"
for s in long_signals:
    report += f"\n  🟢 {s}"
if not long_signals:
    report += "\n  (无)"

report += f"\n\n结论: {verdict}"

if short_score >= 2:
    res = results.get("4h", {}).get("resistance", 65200)
    sup = results.get("1h", {}).get("support", 61500)
    report += f"\n短线阻力{res:.0f} / 支撑{sup:.0f}"

log(report)

# ── 写入通知队列 ──
from notifications import push
push(report.strip(), "btc_check")

print(json.dumps({
    "price": price,
    "verdict": verdict,
    "short_score": short_score,
    "long_score": long_score,
    "kol_sentiment": kol_sent,
}), flush=True)

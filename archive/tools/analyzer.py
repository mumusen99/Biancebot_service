"""
市场分析模块
------------
综合技术分析，输出分析报告供 AI 决策。
优化：批量获取全部币种 ticker，只对重点币做深析。
"""
import json
import logging
from datetime import datetime

from config import ANALYSIS_FILE, STATE_FILE, SYMBOLS, TIMEFRAMES
from data_fetcher import (
    fetch_all_tickers, fetch_positions, fetch_balance,
    fetch_klines, fetch_orderbook,
)
from indicators import generate_technical_signals

logger = logging.getLogger("analyzer")

# 每个分析周期做深析的币种数
DEEP_ANALYSIS_COUNT = 6


def run_analysis() -> dict:
    """
    运行一次完整的市场分析:
    1. 快扫所有币种 (1 次 API 调用)
    2. 选波动最大/最有趣的 6 个做技术深析
    3. 保存到 analysis.json
    """
    logger.info("🔍 开始市场分析...")

    # ─── 1. 批量快扫 ─────────────────────────────────
    all_tickers = fetch_all_tickers()
    logger.info(f"📡 批量获取 {len(all_tickers)} 个币种 ticker")

    # ─── 快扫概览 (所有币种的 24h 基础数据) ────────────
    overview = []
    for sym in SYMBOLS:
        t = all_tickers.get(sym)
        if t:
            overview.append({
                "symbol": sym,
                "price": t["last"],
                "change24h": round(t["change24h"], 2),
                "volume24h": t["volume24h"],
                "high24h": t["high24h"],
                "low24h": t["low24h"],
            })
    # 按 24h 涨跌幅绝对值排序（波动越大越靠前）
    overview.sort(key=lambda x: abs(x["change24h"]), reverse=True)

    # ─── 2. 选重点币做深析 ────────────────────────────
    # 策略：必选 BTC/ETH + 波动最大的 + 持仓的
    must_include = {"BTCUSDT", "ETHUSDT"}
    positions = fetch_positions()
    position_symbols = {p["symbol"] for p in positions}

    deep_symbols = list(must_include)
    for item in overview:
        if item["symbol"] in must_include:
            continue
        if len(deep_symbols) >= DEEP_ANALYSIS_COUNT:
            break
        deep_symbols.append(item["symbol"])

    for ps in position_symbols:
        if ps not in deep_symbols:
            deep_symbols.append(ps)
            if len(deep_symbols) >= DEEP_ANALYSIS_COUNT + 2:
                break

    logger.info(f"🔬 深析 {len(deep_symbols)} 个币: {', '.join(deep_symbols)}")

    # ─── 3. 逐个深析选中的币 ───────────────────────────
    markets = {}
    for symbol in deep_symbols:
        logger.info(f"  深析 {symbol}...")
        t = all_tickers.get(symbol)
        market = {
            "ticker": t,
            "orderbook": fetch_orderbook(None, symbol),
            "technical": {},
        }
        for tf in TIMEFRAMES:
            df = fetch_klines(None, symbol, tf, limit=100)
            if not df.empty:
                signals = generate_technical_signals(df)
                market["technical"][tf] = {
                    **signals,
                    "last_candles": df.tail(3)[["timestamp", "open", "high", "low", "close", "volume"]].to_dict(orient="records"),
                }
            else:
                market["technical"][tf] = {"long_score": 5, "short_score": 5, "signals": ["无数据"]}
        markets[symbol] = market

    # 其余币种只放 ticker 数据
    for item in overview:
        if item["symbol"] not in markets:
            markets[item["symbol"]] = {
                "ticker": item,
                "orderbook": None,
                "technical": {"quick": {"long_score": 5, "short_score": 5, "signals": ["仅快扫，未深析"]}},
            }

    report = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": datetime.now().isoformat(),
        "balance": fetch_balance(None),
        "positions": positions,
        "overview": overview,  # 所有币种快扫
        "deep_symbols": deep_symbols,
        "markets": markets,
        "summary": _generate_summary(markets, overview, positions),
    }

    with open(ANALYSIS_FILE, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    logger.info("✅ 分析完成")
    return report


def _generate_summary(markets: dict, overview: list, positions: list) -> dict:
    """生成综合摘要"""
    total_long = 0
    total_short = 0
    signal_details = []
    trend_votes = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0}

    for symbol, mkt in markets.items():
        for tf, tech in mkt.get("technical", {}).items():
            ls = tech.get("long_score", 5)
            ss = tech.get("short_score", 5)
            total_long += ls
            total_short += ss
            trend = tech.get("trend", "NEUTRAL")
            trend_votes[trend] = trend_votes.get(trend, 0) + 1
            signals = tech.get("signals", [])
            if signals and signals != ["数据不足，信号中性"] and signals != ["无数据"] and signals != ["仅快扫，未深析"]:
                signal_details.append(f"[{symbol} {tf}] {' | '.join(signals[:2])}")

    # 快扫：今日最强/最弱
    sorted_by_change = sorted(overview, key=lambda x: x.get("change24h", 0), reverse=True)
    top_gainers = [s for s in sorted_by_change[:3] if s.get("change24h", 0) > 0]
    top_losers = [s for s in sorted_by_change[-3:] if s.get("change24h", 0) < 0]

    # 投票决定方向
    trend_counts = len([v for v in trend_votes.values()])
    if trend_votes.get("LONG", 0) >= trend_votes.get("SHORT", 0) * 2 and total_long > total_short:
        direction = "LONG"
        confidence = min(10, int(total_long / max(total_short, 1) * 3))
    elif trend_votes.get("SHORT", 0) >= trend_votes.get("LONG", 0) * 2 and total_short > total_long:
        direction = "SHORT"
        confidence = min(10, int(total_short / max(total_long, 1) * 3))
    else:
        direction = "NEUTRAL"
        confidence = 5

    # 找出信号最强的币
    best_long = max(markets.items(), key=lambda kv: sum(
        v.get("long_score", 5) for v in kv[1].get("technical", {}).values()
    )) if markets else ("?", None)
    best_short = max(markets.items(), key=lambda kv: sum(
        v.get("short_score", 5) for v in kv[1].get("technical", {}).values()
    )) if markets else ("?", None)

    has_position = len(positions) > 0

    return {
        "direction": direction,
        "confidence": confidence,
        "total_long_score": total_long,
        "total_short_score": total_short,
        "trend_votes": trend_votes,
        "has_position": has_position,
        "current_positions": [f"{p['symbol']} {p['side']} {p['size']}张 PnL:{p['pnl_percent']:+.2f}%" for p in positions],
        "key_signals": signal_details[:10],
        "top_gainers": [f"{s['symbol']} +{s['change24h']}%" for s in top_gainers],
        "top_losers": [f"{s['symbol']} {s['change24h']}%" for s in top_losers],
        "best_long_candidate": f"{best_long[0]}" if best_long else "",
        "best_short_candidate": f"{best_short[0]}" if best_short else "",
        "advice": _advice_text(direction, confidence, has_position),
    }


def _advice_text(direction: str, confidence: int, has_position: bool) -> str:
    if direction == "LONG" and confidence >= 6:
        return "📈 偏多信号明显，可考虑开多"
    elif direction == "SHORT" and confidence >= 6:
        return "📉 偏空信号明显，可考虑开空"
    elif direction == "NEUTRAL":
        return "➖ 持仓观望" if has_position else "➖ 信号中性，建议观望"
    return "➖ 暂无明确建议"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    run_analysis()

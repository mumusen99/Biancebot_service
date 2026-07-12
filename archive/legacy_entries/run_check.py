#!/usr/bin/env python3
"""
Binance 交易助手 — 单次执行入口
=================================
用法:
  ./run_check.py --analyze       只分析市场
  ./run_check.py --status        查看状态+持仓
  ./run_check.py --execute       执行决策 (从 state.json)
  ./run_check.py --close SYMBOL  平仓
  ./run_check.py --quick         快速行情 (所有币 ticker)
"""
import sys
import json
import logging

from config import STATE_FILE, ANALYSIS_FILE, SYMBOLS, IS_TESTNET, MAX_POSITION_USDT
from data_fetcher import fetch_all_tickers, fetch_positions, fetch_balance
from analyzer import run_analysis
from trader import Trader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def cmd_analyze():
    run_analysis()


def cmd_quick():
    """快速扫描所有币种"""
    tickers = fetch_all_tickers()
    print(f"\n{'='*55}")
    print(f"  📡 快速行情 ({len(tickers)} 币种)")
    print(f"{'='*55}")
    sorted_t = sorted(tickers.items(), key=lambda kv: abs(kv[1].get("change24h", 0)), reverse=True)
    for sym, t in sorted_t:
        if sym not in SYMBOLS:
            continue
        chg = t.get("change24h", 0)
        arrow = "🟢" if chg > 0 else "🔴" if chg < 0 else "⚪"
        print(f"  {arrow} {sym:9s} ${t['last']:>10,.4f}  {chg:+.2f}%")
    print(f"{'='*55}\n")


def cmd_status():
    """查看状态"""
    tickers = fetch_all_tickers()
    bal = fetch_balance()
    positions = fetch_positions()

    print(f"\n{'='*55}")
    print(f"  📊 账户状态")
    print(f"{'='*55}")
    print(f"  💰  余额: {bal['free']:.2f} USDT (可用) / {bal['total']:.2f} (总计)")

    if positions:
        print(f"\n  📦 持仓:")
        for p in positions:
            emoji = "🟢" if p["pnl"] >= 0 else "🔴"
            tag = " [模拟]" if p.get("paper") else ""
            print(f"  {emoji} {p['symbol']:9s} {p['side']:5s} {p['size']:>8.4f}张 | "
                  f"入场:{p['entry_price']:.4f} | PnL:{p['pnl_percent']:+.2f}% ({p['pnl']:+.2f} USDT){tag}")
    else:
        print(f"\n  📭 无持仓")

    # 模拟持仓 (如果有)
    if False:  # 纯实盘（死代码，保留仅防误删）
        pass
        # trader = Trader()
        # paper_pnls = trader.get_paper_pnl()
        for pp in paper_pnls:
            if not any(p.get("paper") for p in positions if p["symbol"] == pp["symbol"]):
                emoji = "🟢" if pp["pnl"] >= 0 else "🔴"
                print(f"  {emoji} {pp['symbol']:9s} {pp['side']:5s}  [模拟] PnL:{pp['pnl']:+.2f} USDT")

    # 快速行情
    print(f"\n  📡 币种快照 (按波动排序):")
    sorted_t = sorted(tickers.items(), key=lambda kv: abs(kv[1].get("change24h", 0)), reverse=True)
    count = 0
    for sym, t in sorted_t:
        if sym not in SYMBOLS:
            continue
        chg = t.get("change24h", 0)
        arrow = "🟢" if chg > 1 else "🔴" if chg < -1 else "⚪"
        print(f"  {arrow} {sym:9s} ${t['last']:>10,.4f}  {chg:+.2f}%", end="")
        count += 1
        if count % 2 == 0:
            print()
        else:
            print("  ", end="")
    if count % 2 != 0:
        print()

    # 分析摘要
    try:
        with open(ANALYSIS_FILE) as f:
            analysis = json.load(f)
        s = analysis.get("summary", {})
        print(f"\n  🔍 {s.get('time', '?')}")
        print(f"  方向: {s.get('direction', '?')} / 置信度: {s.get('confidence', 0)}/10")
        if s.get("top_gainers"):
            print(f"  涨幅榜: {' | '.join(s['top_gainers'][:3])}")
        if s.get("top_losers"):
            print(f"  跌幅榜: {' | '.join(s['top_losers'][:3])}")
        print(f"  建议: {s.get('advice', '?')}")
    except:
        pass

    print(f"{'='*55}\n")


def cmd_execute():
    """执行决策"""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except:
        logger.warning("⚠️ state.json 不存在")
        return

    decision = state.get("pending_decision")
    if not decision:
        logger.info("ℹ️ 无待执行决策")
        return

    logger.info(f"📋 执行: {json.dumps(decision, ensure_ascii=False)}")

    action = decision.get("action", "").upper()
    symbol = decision.get("symbol", "BTCUSDT")
    side = decision.get("side", "").upper()
    usdt_amount = decision.get("usdt_amount", MAX_POSITION_USDT)
    reason = decision.get("reason", "")

    trader = Trader()

    if action == "OPEN":
        if side == "LONG":
            trader.open_long(symbol, usdt_amount, reason)
        elif side == "SHORT":
            trader.open_short(symbol, usdt_amount, reason)
        else:
            logger.error(f"❌ 未知方向: {side}")

    elif action == "CLOSE":
        trader.close_position(symbol)

    elif action == "CLOSE_ALL":
        for sym in SYMBOLS:
            trader.close_position(sym)

    else:
        logger.warning(f"⚠️ 未知 action: {action}")

    # 清除决策 (重新读取 state 避免覆盖 trader 写入的 paper_positions)
    try:
        with open(STATE_FILE) as f:
            new_state = json.load(f)
    except:
        new_state = state
    new_state["pending_decision"] = None
    new_state["last_result"] = {
        "executed_at": __import__("datetime").datetime.now().isoformat(),
        "action": action,
        "symbol": symbol,
    }
    with open(STATE_FILE, "w") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=2)


def cmd_close(symbol: str):
    Trader().close_position(symbol.upper())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1]
    cmds = {
        "--analyze": cmd_analyze,
        "--status": cmd_status,
        "--execute": cmd_execute,
        "--quick": cmd_quick,
    }
    if cmd == "--close" and len(sys.argv) > 2:
        cmd_close(sys.argv[2])
    elif cmd in cmds:
        cmds[cmd]()
    else:
        print(f"未知: {cmd}")
        print(__doc__)

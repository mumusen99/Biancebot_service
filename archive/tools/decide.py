#!/usr/bin/env python3
"""
AI 决策写入工具
================
AI (我) 通过这个脚本把交易决策写入 state.json，然后 run_check.py --execute 执行。
"""
import sys
import json
from datetime import datetime
from config import STATE_FILE


def write_decision(action: str, symbol: str = "BTCUSDT", side: str = "",
                   usdt_amount: float = 100, reason: str = ""):
    """写入一个交易决策"""

    decision = {
        "action": action.upper(),
        "symbol": symbol.upper(),
        "side": side.upper(),
        "usdt_amount": usdt_amount,
        "reason": reason,
        "created_at": datetime.now().isoformat(),
    }

    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                state = json.load(f)
        else:
            state = {"trades": [], "last_trade": None}
    except:
        state = {"trades": [], "last_trade": None}

    state["pending_decision"] = decision

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print(f"✅ 决策已写入: {json.dumps(decision, ensure_ascii=False)}")
    return decision


def write_ai_analysis(
    symbol: str,
    direction: str,
    confidence: int,
    detailed_reason: str,
    market_context: str,
):
    """AI 写入详细分析说明"""
    analysis = {
        "type": "ai_decision",
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "detailed_reason": detailed_reason,
        "market_context": market_context,
        "timestamp": datetime.now().isoformat(),
    }

    try:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                state = json.load(f)
        else:
            state = {}
    except:
        state = {}

    state["ai_analysis"] = analysis
    # 保留历史记录
    if "ai_history" not in state:
        state["ai_history"] = []
    state["ai_history"].append(analysis)
    state["ai_history"] = state["ai_history"][-20:]  # 保留最近20条

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print(f"✅ AI 分析已写入")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法:")
        print("  写入开仓决策:")
        print(f"    python3 {sys.argv[0]} open BTCUSDT LONG 100 'EMA金叉+RSI超卖'")
        print()
        print("  写入平仓决策:")
        print(f"    python3 {sys.argv[0]} close BTCUSDT '' 0 '已到止盈'")
        print()
        sys.exit(1)

    action = sys.argv[1]
    symbol = sys.argv[2] if len(sys.argv) > 2 else "BTCUSDT"
    side = sys.argv[3] if len(sys.argv) > 3 else ""
    usdt_amount = float(sys.argv[4]) if len(sys.argv) > 4 else 100
    reason = sys.argv[5] if len(sys.argv) > 5 else ""

    write_decision(action, symbol, side, usdt_amount, reason)

    # 如果附带详细分析，也写入
    if len(sys.argv) > 6:
        write_ai_analysis(symbol, side, 5, reason, sys.argv[6])

#!/usr/bin/env python3
"""汇报当前持仓摘要"""
import json
from pathlib import Path

BOT_FILE = Path(__file__).parent / "bot_state.json"

def report():
    if not BOT_FILE.exists():
        print("😴 bot_state.json 不存在")
        return
    
    s = json.loads(BOT_FILE.read_text())
    pos = s.get("positions", {})

    if pos:
        print("📊 当前持仓:")
        for sym, p in sorted(pos.items()):
            pnl = p.get("pnl_percent", 0)
            pnl_f = p.get("pnl", 0)
            emoji = "📗" if isinstance(pnl, (int,float)) and pnl >= 0 else "📕"
            side = p.get("side", "?")
            size = p.get("size", "?")
            entry = p.get("entry_price", "?")
            print(f"  {emoji} {sym} {side} {size}张 入场{entry} PnL {pnl:+.2f}% ({pnl_f:+.2f}U)")
        total = s.get("total_pnl", 0)
        used = sum(p.get("amount", 0) for p in pos.values())
        print(f"   预算 {used:.1f}/20U | 总盈亏 {total:+.2f}U")
    else:
        print("😴 无持仓")

if __name__ == "__main__":
    report()

#!/usr/bin/env python3
"""通知模块"""
import subprocess, os as _os, sys, json
from datetime import datetime
from pathlib import Path

QQ_TARGET = "qqbot:5B658C581EF8930B4DE584433B3D130F"
QUEUE_FILE = Path(__file__).parent / "notifications.json"

def _send_qq(msg: str) -> bool:
    try:
        clean = {k: v for k, v in _os.environ.items() if not k.startswith("HERMES_")}
        r = subprocess.run(["hermes", "send", "-t", QQ_TARGET, msg],
                           capture_output=True, text=True, timeout=10, env=clean)
        return r.returncode == 0
    except:
        return False

def _get_account_summary() -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from trading_bot.exchange.gateway import get_gateway
        from trading_bot.exchange.market_data import fetch_balance
        g = get_gateway()
        bal = fetch_balance()
        total = bal.get("total", 0)
        positions = g.get_positions()
        pos_str = ""
        total_pnl = 0.0
        for p in positions:
            pnl = float(p.unrealized_pnl)
            total_pnl += pnl
            margin = float(p.position_amt) * float(p.entry_price) / 5
            pnl_pct = (pnl / margin * 100) if margin > 0 else 0
            pos_str += f"\n  {p.symbol:12s} {p.position_side.value:5s} {float(p.position_amt):>7.1f}张  PnL {pnl:>+7.2f}U ({pnl_pct:>+5.1f}%)"
        if not pos_str:
            pos_str = "\n  (空仓)"
        return f"💰 总资金: {total:.1f}U  持仓盈亏: {total_pnl:+.2f}U\n📊 持仓 {len(positions)}个:{pos_str}"
    except:
        return "(账户信息获取失败)"

def notify_entry(symbol, side, entry_price, qty, sl, tp, score, reason):
    emoji = "🟢" if side == "LONG" else "🔴"
    margin = qty * entry_price / 5
    stars = "⭐" * min(5, max(1, int(score / 3)))
    sl_pct = abs(sl - entry_price) / entry_price * 100
    r_dist = abs(entry_price - sl)
    if side == 'LONG':
        tp1 = entry_price + 0.6 * r_dist
        tp2 = entry_price + 1.2 * r_dist
    else:
        tp1 = entry_price - 0.6 * r_dist
        tp2 = entry_price - 1.2 * r_dist
    tp1_pct = abs(tp1 - entry_price) / entry_price * 100
    tp2_pct = abs(tp2 - entry_price) / entry_price * 100
    msg = (f"{emoji} 开仓 {symbol} {side}  {stars} {score:.1f}分\n"
           f"├ 入场: {entry_price:.4f}  {qty}张  保证金 {margin:.1f}U\n"
           f"├ 止损: {sl:.4f} ({sl_pct:.2f}%)\n"
           f"├ 止盈: TP1={tp1:.4f} (+{tp1_pct:.2f}%) TP2={tp2:.4f} (+{tp2_pct:.2f}%)\n"
           f"└ 信号: {reason}\n\n{_get_account_summary()}")
    _send_qq(msg)

def notify_exit(symbol, side, exit_price, pnl=0.0, reason=""):
    pnl_emoji = "✅" if pnl >= 0 else "❌"
    msg = (f"{pnl_emoji} 平仓 {symbol} {side}\n"
           f"├ 出场: {exit_price:.4f}\n"
           f"├ 盈亏: {pnl:+.3f}U\n"
           f"└ 原因: {reason}\n\n{_get_account_summary()}")
    _send_qq(msg)
    print(f"📤 QQ平仓通知: {symbol}")

def push(msg, msg_type="info"):
    """兼容旧接口"""
    _send_qq(msg)

def pop_all():
    return []

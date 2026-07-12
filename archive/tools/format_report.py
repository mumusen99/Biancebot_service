#!/usr/bin/env python3
"""格式化汇报脚本 — 输出标准格式"""
import json, time, hmac, hashlib, urllib.parse
import requests as req
from config import API_KEY, API_SECRET, PROXY

FAPI = "https://fapi.binance.com/fapi/v1"
FAPI_V2 = "https://fapi.binance.com/fapi/v2"
prox = {"http": PROXY, "https": PROXY}
headers = {"X-MBX-APIKEY": API_KEY}

def sign(path, params, base=FAPI):
    p = dict(params); p["timestamp"]=int(time.time()*1000); p["recvWindow"]=10000
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return f"{base}/{path}?{q}&signature={sig}"

def get_algo(sym):
    r = req.get(sign("allAlgoOrders", {"symbol": sym}), headers=headers, proxies=prox, timeout=10)
    return r.json() if isinstance(r.json(), list) else []

def report():
    # 获取持仓
    r = req.get(sign("positionRisk", {}, FAPI_V2), headers=headers, proxies=prox, timeout=10)
    positions = r.json() if r.status_code == 200 else []
    
    lines = []
    lines.append(f"📊 {time.strftime('%H:%M')} 自动盯盘")
    lines.append("")
    
    total_pnl = 0
    has_pos = False
    for p in positions:
        amt = float(p.get("positionAmt", 0))
        if abs(amt) <= 0:
            continue
        has_pos = True
        sym = p["symbol"]
        side = "LONG" if amt > 0 else "SHORT"
        entry = float(p["entryPrice"])
        mark = float(p.get("markPrice", 0))
        upnl = float(p.get("unRealizedProfit", 0))
        total_pnl += upnl
        
        pnl_pct = (mark - entry) / entry * 100 if side == "LONG" else (entry - mark) / entry * 100
        
        # 查止盈止损
        orders = get_algo(sym)
        active = [o for o in orders if o.get("algoStatus") == "NEW"]
        sl_found = next((o for o in active if o["orderType"] == "STOP_MARKET"), None)
        tp_found = next((o for o in active if o["orderType"] == "TAKE_PROFIT_MARKET"), None)
        
        sl_str = f"🛑 {sl_found['triggerPrice']}" if sl_found else "❌"
        tp_str = f"🎯 {tp_found['triggerPrice']}" if tp_found else "❌"
        
        emoji = "📗" if pnl_pct >= 0 else "📕"
        lines.append(f"  {emoji} {sym} {abs(amt)}张 @ {entry} PnL{pnl_pct:+.2f}% | {sl_str} {tp_str}")
    
    if not has_pos:
        lines.append("  😴 无持仓")
    
    lines.append(f"  总盈亏: {total_pnl:+.2f}U")
    
    # 获取通知
    from pathlib import Path
    nf = Path(__file__).parent / "notifications.json"
    if nf.exists():
        notifs = json.loads(nf.read_text())
        if notifs:
            lines.append("")
            lines.append("📌 新事件:")
            for n in notifs:
                lines.append(f"  {n.get('message','')}")
            # 清空
            nf.write_text("[]")
    
    return "\n".join(lines)

if __name__ == "__main__":
    print(report())

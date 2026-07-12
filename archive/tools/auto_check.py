#!/usr/bin/env python3
"""
自动盯盘自检脚本 — 心跳周期任务
==============================
1. 运行 auto_trader (开仓/平仓/追踪止损)
2. 检查通知队列 → 推送
3. 检查所有持仓的止盈止损是否齐全 → 自动补齐
4. 汇报当前状态
"""
import json, time, hmac, hashlib, urllib.parse
from pathlib import Path
import requests as req
from config import API_KEY, API_SECRET, PROXY, STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT, DEFAULT_LEVERAGE

BASE_DIR = Path(__file__).parent
BOT_FILE = BASE_DIR / "bot_state.json"
NOTIF_FILE = BASE_DIR / "notifications.json"

SL_PRICE_PCT = STOP_LOSS_PERCENT / DEFAULT_LEVERAGE
TP_PRICE_PCT = TAKE_PROFIT_PERCENT / DEFAULT_LEVERAGE

FAPI = "https://fapi.binance.com/fapi/v1"
prox = {"http": PROXY, "https": PROXY}
headers = {"X-MBX-APIKEY": API_KEY}

def _sign(path, params):
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 10000
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return f"{FAPI}/{path}?{q}&signature={sig}"

# ─── 精度缓存 ───────────────────────────────────────────
_PRECISION_CACHE = {}

def _load_precision():
    """从 Binance 交易所动态加载所有交易对的 tickSize 和 stepSize"""
    try:
        url = f"{FAPI}/exchangeInfo"
        r = req.get(url, headers=headers, proxies=prox, timeout=10)
        if r.status_code != 200:
            return
        data = r.json()
        for s in data.get("symbols", []):
            sym = s["symbol"]
            tick, step = 0.001, 0.01
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    ss = f["stepSize"].rstrip("0")
                    step = float(f["stepSize"])
                if f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
            _PRECISION_CACHE[sym] = (tick, step)
    except Exception as e:
        print(f"加载精度失败: {e}")

def _get_precision(symbol):
    """获取 tickSize 和 stepSize，优先动态加载"""
    if symbol in _PRECISION_CACHE:
        return _PRECISION_CACHE[symbol]
    # 首次运行加载
    _load_precision()
    if symbol in _PRECISION_CACHE:
        return _PRECISION_CACHE[symbol]
    # 硬编码回退
    fallback = {
        "BTCUSDT": (0.1, 0.001),
        "ETHUSDT": (0.01, 0.001),
        "BNBUSDT": (0.01, 0.001),
        "SOLUSDT": (0.01, 0.01),
        "XRPUSDT": (0.0001, 0.1),
        "DOGEUSDT": (0.00001, 1),
        "DOTUSDT": (0.001, 0.01),
        "ADAUSDT": (0.0001, 0.1),
        "OPUSDT": (0.0001, 0.1),
        "LINKUSDT": (0.001, 0.01),
        "AVAXUSDT": (0.001, 0.01),
        "NEARUSDT": (0.001, 0.01),
        "APTUSDT": (0.001, 0.1),
        "ARBUSDT": (0.001, 0.1),
        "MATICUSDT": (0.0001, 0.1),
        "PEPEUSDT": (0.00000001, 1),
    }
    return fallback.get(symbol, (0.001, 0.01))

def _get_positions():
    """从交易所获取实际持仓 (使用 v2/positionRisk)"""
    FAPI_V2 = "https://fapi.binance.com/fapi/v2"
    p = {"timestamp": int(time.time() * 1000), "recvWindow": 10000}
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{FAPI_V2}/positionRisk?{q}&signature={sig}"
    r = req.get(url, headers=headers, proxies=prox, timeout=10)
    if r.status_code != 200:
        return []
    result = []
    for p in r.json():
        amt = float(p.get("positionAmt", 0))
        if abs(amt) > 0:
            result.append({
                "symbol": p["symbol"], "side": "LONG" if amt > 0 else "SHORT",
                "size": abs(amt), "entry_price": float(p["entryPrice"]),
                "mark_price": float(p.get("markPrice", 0)),
                "pnl": float(p.get("unRealizedProfit", 0)),
                "leverage": int(float(p.get("leverage", 3))),
            })
    return result

def _get_algo_orders(symbol):
    """查询交易所 algo 订单"""
    r = req.get(_sign("allAlgoOrders", {"symbol": symbol}), headers=headers, proxies=prox, timeout=10)
    return r.json() if isinstance(r.json(), list) else []

def _post_algo(params):
    """POST algoOrder"""
    r = req.post(_sign("algoOrder", params), headers=headers, proxies=prox, timeout=15)
    return r.status_code == 200, r.json()

def _delete_algo(symbol, algo_id):
    """取消单个 algo 订单"""
    r = req.delete(_sign("algoOrder", {"symbol": symbol, "algoId": algo_id}),
                   headers=headers, proxies=prox, timeout=10)
    return r.status_code == 200

def _cancel_all_algo(symbol):
    """取消某个币的所有 algo 订单"""
    r = req.delete(_sign("allAlgoOrders", {"symbol": symbol}),
                   headers=headers, proxies=prox, timeout=10)
    return r.status_code == 200

def _check_and_fix_sltp(all_positions):
    """检查每个 position 是否有活跃的 SL 和 TP，缺少就补上"""
    fixes = []
    for pos in all_positions:
        sym = pos["symbol"]
        side = pos["side"]
        entry = pos["entry_price"]
        qty = pos["size"]
        
        # 按5%/3x计算止盈止损价
        if side == "LONG":
            sl_price = entry * (1 - SL_PRICE_PCT / 100)
            tp_price = entry * (1 + TP_PRICE_PCT / 100)
            sl_side = "SELL"
        else:
            sl_price = entry * (1 + SL_PRICE_PCT / 100)
            tp_price = entry * (1 - TP_PRICE_PCT / 100)
            sl_side = "BUY"
        tp_side = sl_side
        
        # 从交易所动态获取精度
        tick, step = _get_precision(sym)
        
        sl_price = round(int(sl_price / tick + 0.5) * tick, 8)
        tp_price = round(int(tp_price / tick + 0.5) * tick, 8)
        
        # 查询现有的 algo 订单
        orders = _get_algo_orders(sym)
        
        # 检查止盈止损是否存在且价格正确（允许0.5%偏差）
        def price_ok(order, expected):
            if not order or order.get("algoStatus") != "NEW":
                return False
            actual = float(order.get("triggerPrice", 0))
            diff = abs(actual - expected) / max(expected, 0.0001)
            return diff < 0.005  # 0.5%以内
        
        sl_orders = [o for o in orders if o.get("orderType") == "STOP_MARKET" and o.get("algoStatus") == "NEW"]
        tp_orders = [o for o in orders if o.get("orderType") == "TAKE_PROFIT_MARKET" and o.get("algoStatus") == "NEW"]
        
        sl_ok = price_ok(sl_orders[0], sl_price) if sl_orders else False
        tp_ok = price_ok(tp_orders[0], tp_price) if tp_orders else False
        
        # 清理多余的或价格不对的
        for lst, expected in [(sl_orders, sl_price), (tp_orders, tp_price)]:
            if len(lst) > 1:
                for o in lst[1:]:
                    _delete_algo(sym, o["algoId"])
                    fixes.append(f"{sym} 清理重复订单")
                    time.sleep(0.2)
        
        # 数量精度（从 _get_precision 获取）
        qty_fmt = round(int(qty / step) * step, 8)
        
        if not sl_ok:
            # 先取消旧止损
            if sl_orders:
                _delete_algo(sym, sl_orders[0]["algoId"])
                time.sleep(0.3)
            ok, _ = _post_algo({
                "symbol": sym, "side": sl_side, "positionSide": side,
                "algotype": "CONDITIONAL", "type": "STOP_MARKET",
                "quantity": qty_fmt, "triggerprice": sl_price,
                "workingType": "MARK_PRICE",
            })
            fixes.append(f"{sym} 止损 {'✅' if ok else '❌'} @ {sl_price}")
            time.sleep(0.3)
            
        if not tp_ok:
            if tp_orders:
                _delete_algo(sym, tp_orders[0]["algoId"])
                time.sleep(0.3)
            ok, _ = _post_algo({
                "symbol": sym, "side": tp_side, "positionSide": side,
                "algotype": "CONDITIONAL", "type": "TAKE_PROFIT_MARKET",
                "quantity": qty_fmt, "triggerprice": tp_price,
                "workingType": "MARK_PRICE",
            })
            fixes.append(f"{sym} 止盈 {'✅' if ok else '❌'} @ {tp_price}")
            time.sleep(0.3)
    
    return fixes

def _cleanup_stale_algo_orders(all_positions):
    """清理过期条件委托：已平仓的币上的止盈止损单全部取消"""
    lines = []
    try:
        # 获取所有有持仓的币
        live_syms = {p["symbol"] for p in all_positions}
        
        # 获取所有历史接触过的币（从 state.json 读取 + 配置币种）
        from config import SYMBOLS
        check_syms = set(SYMBOLS)
        
        # 从 bot_state 读取历史持仓
        try:
            if BOT_FILE.exists():
                bot_state = json.loads(BOT_FILE.read_text())
                check_syms.update(bot_state.get("positions", {}).keys())
                check_syms.update(bot_state.get("live_exchange_symbols", []))
        except:
            pass
        
        cleaned = 0
        for sym in check_syms:
            if sym in live_syms:
                continue  # 有持仓，跳过
            # 查这个币上有没有残留的 algo 订单
            orders = _get_algo_orders(sym)
            active = [o for o in orders if o.get("algoStatus") == "NEW" or o.get("algoStatus") == "PARTIALLY_FILLED"]
            if not active:
                continue
            # 取消全部
            count = len(active)
            for o in active:
                _delete_algo(sym, o["algoId"])
                time.sleep(0.1)
            lines.append(f"  🧹 {sym} 清理 {count} 个过期委托")
            cleaned += count
        
        if cleaned > 0:
            lines.append(f"  ✅ 共清理 {cleaned} 个过期条件委托")
    except Exception as e:
        lines.append(f"  ⚠️ 清理过期委托异常: {e}")
    return lines


def check_and_report():
    """完整自检流程"""
    lines = []
    
    # 0. 清理过期条件委托
    lines.append(f"📊 自动盯盘 - {time.strftime('%H:%M')}")
    
    # 1. 获取交易所持仓
    positions = _get_positions()
    
    # 清理未平仓币上的过期委托
    cleanup_lines = _cleanup_stale_algo_orders(positions)
    lines.extend(cleanup_lines)
    
    if not positions:
        lines.append("😴 无持仓")
        return "\n".join(lines)
    
    # 2. 检查并补齐止盈止损
    fixes = _check_and_fix_sltp(positions)
    if fixes:
        for f in fixes:
            lines.append(f"  {f}")
    
    # 3. 获取最新 SL/TP 状态
    lines.append("")
    total_pnl = 0
    for pos in positions:
        sym = pos["symbol"]
        orders = _get_algo_orders(sym)
        active = [o for o in orders if o.get("algoStatus") == "NEW"]
        entry = pos["entry_price"]
        pnl_pct = (pos["mark_price"] - entry) / entry * 100 if pos["side"] == "LONG" else (entry - pos["mark_price"]) / entry * 100
        total_pnl += pos["pnl"]
        
        sl_line = "❌"
        tp_line = "❌"
        for o in active:
            if o["orderType"] == "STOP_MARKET":
                sl_line = f"🛑 {o['triggerPrice']}"
            elif o["orderType"] == "TAKE_PROFIT_MARKET":
                tp_line = f"🎯 {o['triggerPrice']}"
        
        emoji = "📗" if pnl_pct >= 0 else "📕"
        lines.append(f"  {emoji} {sym} {pos['size']}张 @ {entry} PnL{pnl_pct:+.2f}% | {sl_line} {tp_line}")
    
    lines.append(f"  总盈亏: {total_pnl:+.2f}U")
    return "\n".join(lines)

if __name__ == "__main__":
    print(check_and_report())

"""检查全部挂单+持仓，标记不合适的"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _g(path, params=None):
    for i in range(8):
        try:
            p = dict(params or {})
            p['timestamp'] = int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.get(f'{FAPI}/{path}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code != 200: return None
            return r.json()
        except: time.sleep(5)
    return None

def _d(path, params=None):
    for i in range(8):
        try:
            p = dict(params or {})
            p['timestamp'] = int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.delete(f'{FAPI}/{path}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,): return None
            return r.json()
        except: time.sleep(5)
    return None

print('=== 📋 当前挂单 ===', flush=True)
orders = _g('openOrders')
open_orders_info = []
if orders:
    for o in orders:
        sym = o['symbol']
        side = o['side']
        pside = o.get('positionSide','')
        price = o['price']
        qty = o['origQty']
        typ = o['type']
        oid = o['orderId']
        filled = o.get('executedQty','0')
        print(f'{sym} {side} {pside} {typ} @{price} x{qty} 已成交{filled} orderId={oid}', flush=True)
        open_orders_info.append(o)
else:
    print('(无挂单)', flush=True)

print(f'\n=== 📊 当前持仓 ===', flush=True)
pos_list = _g('https://fapi.binance.com/fapi/v2/positionRisk')
positions = []
if pos_list:
    for p in pos_list:
        amt = float(p.get('positionAmt',0))
        if amt == 0: continue
        entry = float(p.get('entryPrice',0))
        mark = float(p.get('markPrice',0))
        upnl = float(p.get('unRealizedProfit',0))
        side = 'LONG' if amt > 0 else 'SHORT'
        pnl_pct = ((mark/entry)-1)*100 * (1 if amt>0 else -1)
        liq = p.get('liquidationPrice','0')
        positions.append(p)
        print(f'{p["symbol"]} {side} {abs(amt):.4f} 入场{entry:.4f} 现价{mark:.4f} PnL{upnl:+.2f}U({pnl_pct:+.2f}%) 强平{liq}', flush=True)
else:
    print('(获取失败)', flush=True)

print(f'\n=== 🔍 评估 ===', flush=True)

# Evaluate open orders
for o in open_orders_info:
    sym = o['symbol']
    price = float(o['price'])
    typ = o['type']
    
    # Get current price
    d = _g('ticker/price', {'symbol': sym})
    cur = float(d['price']) if d and 'price' in d else 0
    
    if typ == 'LIMIT' and o['side'] == 'BUY':
        gap = (cur/price - 1) * 100
        print(f'{sym} 限价买单 @{price} 市价{cur:.2f} 差距{gap:+.1f}%', flush=True)
        if gap > 10:
            print(f'  ⚠️ 市价比挂单价高{gap:.0f}%，短期不太可能成交，建议撤单', flush=True)
        elif gap < 1:
            print(f'  ⚠️ 市价{gap:+.1f}%，即将成交，留意', flush=True)
        else:
            print(f'  ✅ 合适，等回调{gap:.1f}%后成交', flush=True)

# Evaluate positions
for p in positions:
    sym = p['symbol']
    amt = float(p.get('positionAmt',0))
    entry = float(p.get('entryPrice',0))
    mark = float(p.get('markPrice',0))
    upnl = float(p.get('unRealizedProfit',0))
    side = 'LONG' if amt > 0 else 'SHORT'
    pnl_pct = ((mark/entry)-1)*100 * (1 if amt>0 else -1)
    
    # SL/TP check - query algo orders
    alog = _g('algoOrder', {'symbol': sym, 'algoStatus': 'NEW'})
    has_sl = False
    has_tp = False
    if alog and isinstance(alog, list):
        for a in alog:
            atype = a.get('orderType','')
            if 'STOP' in atype.upper() and not 'TAKE' in atype.upper():
                has_sl = True
            elif 'TAKE' in atype.upper():
                has_tp = True
    
    if side == 'LONG':
        sl_note = f'SL:{"✅" if has_sl else "❌"} TP:{"✅" if has_tp else "❌"}'
    else:  # SHORT
        sl_note = f'SL:{"✅" if has_sl else "❌"} TP:{"✅" if has_tp else "❌"}'
    
    print(f'{sym} {side} PnL{pnl_pct:+.2f}% {sl_note}', flush=True)
    
    if pnl_pct < -5:
        print(f'  ⚠️ 亏损超过5%，需要关注')
    if not has_sl or not has_tp:
        print(f'  ⚠️ 止损/止盈未设置!')

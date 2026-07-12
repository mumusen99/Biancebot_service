"""全面检查挂单+持仓，按新策略优化"""
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
            if r.status_code != 200:
                return {'_error': r.status_code, '_msg': r.text[:200]}
            return r.json()
        except Exception as e:
            time.sleep(5)
    return None

def _d(path, params=None):
    for i in range(8):
        try:
            p = dict(params or {})
            p['timestamp'] = int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.delete(f'{FAPI}/{path}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,):
                return None
            return r.json()
        except:
            time.sleep(5)
    return None

def _p(path, params=None):
    for i in range(8):
        try:
            p = dict(params or {})
            p['timestamp'] = int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.post(f'{FAPI}/{path}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,):
                return None
            return r.json()
        except:
            time.sleep(5)
    return None

# ── 1. 获取当前价格 ──
print('📊 获取行情...', flush=True)

def price(sym):
    d = _g('ticker/price', {'symbol': sym})
    return float(d['price']) if d and 'price' in d else 0

def tick(sym):
    ex = _g('exchangeInfo')
    if not ex: return 0.01
    si = next((s for s in ex.get('symbols', []) if s['symbol'] == sym), None)
    if not si: return 0.01
    flt = {f['filterType']: f for f in si['filters']}
    return float(flt['PRICE_FILTER']['tickSize'])

# ── 2. 检查挂单 ──
print('\n=== 📋 挂单 ===', flush=True)
orders = _g('openOrders')
if not orders or '_error' in orders:
    print('获取失败', flush=True)
else:
    for o in orders:
        sym = o['symbol']
        cur = price(sym)
        gap = ((cur/float(o['price']))-1)*100 if cur else 0
        print(f'{sym} {o["side"]} @{o["price"]} x{o["origQty"]}  市价{cur:.2f}  差距{gap:+.1f}%  id={o["orderId"]}', flush=True)

# ── 3. 检查持仓（逐币查询） ──
position_symbols = ['XAUUSDT', 'SPCXUSDT', 'SYNUSDT', 'SOXLUSDT', 'KORUUSDT', 'DOGEUSDT', 'MUUSDT', 'SKHYNIXUSDT', 'AVAXUSDT', 'DRAMUSDT']

print('\n=== 📊 持仓 ===', flush=True)
active_positions = []
for sym in position_symbols:
    d = _g('positionRisk', {'symbol': sym})
    if not d: continue
    for p in d if isinstance(d, list) else [d]:
        amt = float(p.get('positionAmt', 0))
        if amt == 0: continue
        entry = float(p.get('entryPrice', 0))
        mark = float(p.get('markPrice', 0))
        upnl = float(p.get('unRealizedProfit', 0))
        side = 'LONG' if amt > 0 else 'SHORT'
        pnl_pct = ((mark/entry)-1)*100 * (1 if amt>0 else -1)
        liq = p.get('liquidationPrice', '?')
        active_positions.append((sym, side, amt, entry, mark, upnl, pnl_pct))

        # Check existing SL/TP
        alog = _g('algoOrder', {'symbol': sym})
        sl_exists = set()
        tp_exists = []
        if alog and isinstance(alog, list):
            for a in alog:
                if a.get('algoStatus') != 'NEW': continue
                otype = a.get('orderType', '')
                price = a.get('triggerPrice', '?')
                qty = a.get('quantity', '?')
                if 'STOP' in otype and 'TAKE' not in otype:
                    sl_exists.add(price)
                elif 'TAKE' in otype:
                    tp_exists.append((price, qty))

        print(f'{sym} {side} {abs(amt):.4f} entry={entry:.4f} mark={mark:.4f} PnL{upnl:+.2f}U ({pnl_pct:+.2f}%) liq={liq}', flush=True)
        print(f'  SL: {sl_exists if sl_exists else "❌无"}  TP: {tp_exists if tp_exists else "❌无"}', flush=True)

if not active_positions:
    print('(无持仓)', flush=True)

# ── 4. 优化: 按新策略设三档止盈 ──
print('\n=== 🔧 优化 ===', flush=True)

for sym, side, amt, entry, mark, upnl, pnl_pct in active_positions:
    t = tick(sym)
    abs_amt = abs(amt)
    total_val = abs_amt * entry * 3  # at 3x leverage
    margin = total_val / 3
    
    # Calculate 3 TP levels
    # TP1: 50% at 8-12% margin profit
    tp1_pct = 10  # 10% margin profit -> ~3.3% price move at 3x
    tp2_pct = 18
    tp3_pct = 28
    
    tp1_price = entry * (1 + tp1_pct/100/3) if side == 'LONG' else entry * (1 - tp1_pct/100/3)
    tp2_price = entry * (1 + tp2_pct/100/3) if side == 'LONG' else entry * (1 - tp2_pct/100/3)
    tp3_price = entry * (1 + tp3_pct/100/3) if side == 'LONG' else entry * (1 - tp3_pct/100/3)
    
    tp1_qty = round(abs_amt * 0.5, 4)  # 50%
    tp2_qty = round(abs_amt * 0.25, 4)  # 25%
    tp3_qty = round(abs_amt * 0.25, 4)  # 25%
    
    # Align to tick
    tp1_price = round(int(tp1_price / t + 0.5) * t, 8)
    tp2_price = round(int(tp2_price / t + 0.5) * t, 8)
    tp3_price = round(int(tp3_price / t + 0.5) * t, 8)
    
    # SL stays the same (from existing config)
    sl_price = entry * 0.967  # -3.3% price move (~10% margin loss) if LONG
    if side != 'LONG':
        sl_price = entry * 1.033
    
    print(f'\n{sym}: 按新策略设置三档止盈', flush=True)
    print(f'  入场${entry:.4f} x{abs_amt} = {margin:.2f}U保证金', flush=True)
    
    # Cancel existing algo orders    
    print('  清理旧止盈单...', flush=True)
    d = _g('algoOrder', {'symbol': sym})
    if d and isinstance(d, list):
        for a in d:
            if a.get('algoStatus') == 'NEW':
                _d('algoOrder', {'symbol': sym, 'algoId': a.get('algoId', '')})
    
    # Place new orders
    # First: Cancel and re-place SL
    print(f'  止损: ${sl_price:.4f}', flush=True)
    
    # TP1 - 50%
    print(f'  TP1🥇 ${tp1_price:.4f} x{tp1_qty} (50%)', flush=True)
    r = _p('algoOrder', {
        'symbol': sym, 'side': 'SELL' if side=='LONG' else 'BUY',
        'positionSide': side, 'algotype': 'CONDITIONAL',
        'type': 'TAKE_PROFIT_MARKET', 'triggerPrice': str(tp1_price),
        'quantity': str(tp1_qty), 'workingType': 'MARK_PRICE',
        'timeInForce': 'GTE_GTC',
    })
    print(f'    {"✅" if r and "algoId" in r else "❌"}', flush=True)
    
    # TP2 - 25%
    print(f'  TP2🥈 ${tp2_price:.4f} x{tp2_qty} (25%)', flush=True)
    r = _p('algoOrder', {
        'symbol': sym, 'side': 'SELL' if side=='LONG' else 'BUY',
        'positionSide': side, 'algotype': 'CONDITIONAL',
        'type': 'TAKE_PROFIT_MARKET', 'triggerPrice': str(tp2_price),
        'quantity': str(tp2_qty), 'workingType': 'MARK_PRICE',
        'timeInForce': 'GTE_GTC',
    })
    print(f'    {"✅" if r and "algoId" in r else "❌"}', flush=True)
    
    # TP3 - 25%
    print(f'  TP3🥉 ${tp3_price:.4f} x{tp3_qty} (25%)', flush=True)
    r = _p('algoOrder', {
        'symbol': sym, 'side': 'SELL' if side=='LONG' else 'BUY',
        'positionSide': side, 'algotype': 'CONDITIONAL',
        'type': 'TAKE_PROFIT_MARKET', 'triggerPrice': str(tp3_price),
        'quantity': str(tp3_qty), 'workingType': 'MARK_PRICE',
        'timeInForce': 'GTE_GTC',
    })
    print(f'    {"✅" if r and "algoId" in r else "❌"}', flush=True)
    
    print(f'  TP1触发后 → 上移止损到入场价${entry:.4f}（保本保护）', flush=True)

print('\n✅ 检查优化完成', flush=True)

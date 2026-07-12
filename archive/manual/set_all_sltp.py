"""为4个新成交设置止盈止损"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _call(method, path, params=None, retries=8):
    for i in range(retries):
        try:
            p = dict(params or {})
            p['timestamp']=int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f'{FAPI}/{path}?{q}&signature={sig}'
            if method == 'POST':
                r = req.post(url, headers=hdrs, timeout=20, proxies=prox)
            else:
                r = req.get(url, headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,): continue
            return r.json()
        except: time.sleep(5)
    return None

# Get tick sizes for each symbol
ex = _call('GET', 'exchangeInfo')
tick_map = {}
step_map = {}
if ex:
    for s in ex.get('symbols', []):
        sym = s['symbol']
        flt = {f['filterType']: f for f in s['filters']}
        tick_map[sym] = float(flt['PRICE_FILTER']['tickSize'])
        step_map[sym] = float(flt['LOT_SIZE']['stepSize'])

def align_price(sym, p):
    t = tick_map.get(sym, 0.01)
    return round(int(p/t+0.5)*t, 8)

def align_qty(sym, q):
    step = step_map.get(sym, 0.001)
    ss = str(step)
    dec = len(ss.split('.')[1].rstrip('0')) if '.' in ss else 0
    q = round(int(q/step)*step, dec)
    return q

# Positions needing SL/TP
# (sym, entry, qty, side, lev, note)
jobs = [
    ('KORUUSDT', 520.00, 0.19, 'LONG', 5, '已盈利+5.9%'),
    ('DRAMUSDT', 58.95, 0.64, 'LONG', 3, '浮盈+3.2%'),
    ('MUUSDT', 917.56, 0.01, 'LONG', 3, '浮盈+1.8%'),
    ('SPCXUSDT', 155.18, 0.93, 'LONG', 5, '浮亏-2.1%'),
]

for sym, entry, qty, side, lev, note in jobs:
    print(f'\n=== {sym} {note} ===', flush=True)
    
    # SL: 10% margin loss = 10/lev % price
    sl_pct = 10 / lev
    sl_price = entry * (1 - sl_pct/100) if side == 'LONG' else entry * (1 + sl_pct/100)
    
    # For KORU, set SL to entry price (breakeven) since it's already profitable
    if sym == 'KORUUSDT':
        sl_price = entry  # breakeven
    
    # TPs: 10%, 18%, 28% margin profit
    tp1_pct = 10 / lev
    tp2_pct = 18 / lev
    tp3_pct = 28 / lev
    
    tp1 = entry * (1 + tp1_pct/100) if side == 'LONG' else entry * (1 - tp1_pct/100)
    tp2 = entry * (1 + tp2_pct/100) if side == 'LONG' else entry * (1 - tp2_pct/100)
    tp3 = entry * (1 + tp3_pct/100) if side == 'LONG' else entry * (1 - tp3_pct/100)
    
    # Align prices
    sl_price = align_price(sym, sl_price)
    tp1 = align_price(sym, tp1)
    tp2 = align_price(sym, tp2)
    tp3 = align_price(sym, tp3)
    
    # Quantities
    q50 = align_qty(sym, qty * 0.5)
    q25 = align_qty(sym, qty * 0.25)
    
    # For KORU: since TP1/TP2 already exceeded, just set breakeven SL and a trailing TP
    if sym == 'KORUUSDT':
        print(f'  KORU已盈利, 设保本止损${sl_price:.2f} + 一个高位止盈', flush=True)
        # Set SL at entry (breakeven)
        r = _call('POST', 'algoOrder', {
            'symbol': sym, 'side': 'SELL', 'positionSide': 'LONG',
            'algotype': 'CONDITIONAL', 'type': 'STOP_MARKET',
            'triggerPrice': str(sl_price), 'quantity': str(qty),
            'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
        })
        print(f'  SL: {"✅" if r and "algoId" in r else "❌"}', flush=True)
        # TP at 600 (nice round number)
        tp = align_price(sym, 600.0)
        r = _call('POST', 'algoOrder', {
            'symbol': sym, 'side': 'SELL', 'positionSide': 'LONG',
            'algotype': 'CONDITIONAL', 'type': 'TAKE_PROFIT_MARKET',
            'triggerPrice': str(tp), 'quantity': str(qty),
            'workingType': 'MARK_PRICE',
        })
        print(f'  TP @${tp}: {"✅" if r and "algoId" in r else "❌"}', flush=True)
        continue
    
    # Standard 3-tier for other positions
    print(f'  止损@{sl_price:.4f} (-{sl_pct:.1f}%价格, 全仓)', flush=True)
    print(f'  TP1@{tp1:.4f} x{q50} (50%)', flush=True)
    print(f'  TP2@{tp2:.4f} x{q25} (25%)', flush=True)
    print(f'  TP3@{tp3:.4f} x{q25} (25%)', flush=True)
    
    # Place SL
    r = _call('POST', 'algoOrder', {
        'symbol': sym, 'side': 'SELL', 'positionSide': 'LONG',
        'algotype': 'CONDITIONAL', 'type': 'STOP_MARKET',
        'triggerPrice': str(sl_price), 'quantity': str(qty),
        'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
    })
    print(f'  SL: {"✅" if r and "algoId" in r else "❌"}', flush=True)
    
    # TP1
    r = _call('POST', 'algoOrder', {
        'symbol': sym, 'side': 'SELL', 'positionSide': 'LONG',
        'algotype': 'CONDITIONAL', 'type': 'TAKE_PROFIT_MARKET',
        'triggerPrice': str(tp1), 'quantity': str(q50),
        'workingType': 'MARK_PRICE',
    })
    print(f'  TP1: {"✅" if r and "algoId" in r else "❌"}', flush=True)
    
    # TP2
    r = _call('POST', 'algoOrder', {
        'symbol': sym, 'side': 'SELL', 'positionSide': 'LONG',
        'algotype': 'CONDITIONAL', 'type': 'TAKE_PROFIT_MARKET',
        'triggerPrice': str(tp2), 'quantity': str(q25),
        'workingType': 'MARK_PRICE',
    })
    print(f'  TP2: {"✅" if r and "algoId" in r else "❌"}', flush=True)
    
    # TP3
    r = _call('POST', 'algoOrder', {
        'symbol': sym, 'side': 'SELL', 'positionSide': 'LONG',
        'algotype': 'CONDITIONAL', 'type': 'TAKE_PROFIT_MARKET',
        'triggerPrice': str(tp3), 'quantity': str(q25),
        'workingType': 'MARK_PRICE',
    })
    print(f'  TP3: {"✅" if r and "algoId" in r else "❌"}', flush=True)

print(f'\n✅ 全部完成', flush=True)

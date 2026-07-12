"""开 ADAUSDT 多单"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _call(method, path, params=None, retries=10):
    for i in range(retries):
        try:
            p = dict(params or {})
            p['timestamp']=int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f'{FAPI}/{path}?{q}&signature={sig}'
            if method == 'GET':
                r = req.get(url, headers=hdrs, timeout=20, proxies=prox)
            else:
                r = req.post(url, headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,):
                return r.json() if r.text else {"_e": str(r.status_code)}
            return r.json()
        except Exception as e:
            if i < retries-1: time.sleep(5)
    return None

# Get precision
ex = _call('GET', 'exchangeInfo')
si = None
for s in ex.get('symbols',[]):
    if s['symbol'] == 'ADAUSDT':
        si = s
        break
if not si:
    print('查不到ADA', flush=True)
    sys.exit(1)
flt = {f['filterType']: f for f in si['filters']}
step = float(flt['LOT_SIZE']['stepSize'])
min_qty = float(flt['LOT_SIZE']['minQty'])
tick = float(flt['PRICE_FILTER']['tickSize'])
print('step=%s min_qty=%s tick=%s' % (step, min_qty, tick), flush=True)

# Price
d = _call('GET', 'ticker/price', {'symbol': 'ADAUSDT'})
price = float(d['price']) if d and 'price' in d else 0
print('ADAUSDT: $%.6f' % price, flush=True)

# Leverage 3x
_call('POST', 'leverage', {'symbol': 'ADAUSDT', 'leverage': 3}, retries=3)

# Quantity: 10U x3 = 30U
raw_qty = 30 / price
ss = str(step)
dec = len(ss.split('.')[1].rstrip('0')) if '.' in ss else 0
qty = int(raw_qty / step) * step
qty = round(qty, dec)
if qty < min_qty or qty * price < 5:
    qty = round(int(max(min_qty, 5/price) / step + 1) * step, dec)
print('数量: %s (价值%.2fU, 保证金%.2fU)' % (qty, qty*price, qty*price/3), flush=True)

# Market order
order = _call('POST', 'order', {
    'symbol': 'ADAUSDT', 'side': 'BUY', 'type': 'MARKET',
    'quantity': str(qty), 'positionSide': 'LONG',
})

if not order or 'orderId' not in order:
    print('下单失败', flush=True)
    sys.exit(1)

entry = float(order.get('avgPrice', price))
filled = float(order.get('executedQty', qty))
print('成交! orderId=%s entry=%.6f filled=%s' % (order['orderId'], entry, filled), flush=True)

# SL/TP
sl = 0.170
tp1 = 0.186
tp2 = 0.192

# Align prices
sl = round(int(sl/tick+0.5)*tick, 8)
tp1 = round(int(tp1/tick+0.5)*tick, 8)
tp2 = round(int(tp2/tick+0.5)*tick, 8)

q50 = round(filled * 0.5, dec)
q50 = round(int(q50/step)*step, dec)
q50 = max(q50, min_qty)

print('SL: $%.4f (-%.1f%%)' % (sl, (1-sl/entry)*100), flush=True)
print('TP1: $%.4f x%s (50%%)' % (tp1, q50), flush=True)
print('TP2: $%.4f x%s (50%%)' % (tp2, q50), flush=True)

# Place SL
r = _call('POST', 'algoOrder', {
    'symbol': 'ADAUSDT', 'side': 'SELL', 'positionSide': 'LONG',
    'algotype': 'CONDITIONAL', 'type': 'STOP_MARKET',
    'triggerPrice': str(sl), 'quantity': str(filled),
    'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
})
sl_ok = r and 'algoId' in r
print('SL: %s' % ('✅' if sl_ok else '❌'), flush=True)

# TP1
r = _call('POST', 'algoOrder', {
    'symbol': 'ADAUSDT', 'side': 'SELL', 'positionSide': 'LONG',
    'algotype': 'CONDITIONAL', 'type': 'TAKE_PROFIT_MARKET',
    'triggerPrice': str(tp1), 'quantity': str(q50),
    'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
})
print('TP1: %s' % ('✅' if r and 'algoId' in r else '❌'), flush=True)

# TP2
r = _call('POST', 'algoOrder', {
    'symbol': 'ADAUSDT', 'side': 'SELL', 'positionSide': 'LONG',
    'algotype': 'CONDITIONAL', 'type': 'TAKE_PROFIT_MARKET',
    'triggerPrice': str(tp2), 'quantity': str(q50),
    'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
})
print('TP2: %s' % ('✅' if r and 'algoId' in r else '❌'), flush=True)

print('\n=== 开单完成 ===', flush=True)
print('ADAUSDT LONG %s @ %.4f' % (filled, entry), flush=True)
print('SL: %.4f | TP1: %.4f(50%%) | TP2: %.4f(50%%)' % (sl, tp1, tp2), flush=True)
print('保证金: %.2fU' % (qty*price/3), flush=True)

# Save for notifications
from notifications import push
push('🚀 开多: ADAUSDT LONG\n入场: $%.4f\n数量: %s\n保证金: %.2fU\nSL: $%.4f\nTP1: $%.4f(50%%)\nTP2: $%.4f(50%%)' % (entry, filled, qty*price/3, sl, tp1, tp2), 'open')

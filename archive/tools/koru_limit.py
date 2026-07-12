"""KORUUSDT 限价单 $520 20U 5x"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _call(method, path, params=None):
    for i in range(12):
        try:
            p = dict(params or {})
            p['timestamp'] = int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f'{FAPI}/{path}?{q}&signature={sig}'
            r = req.post(url, headers=hdrs, timeout=20, proxies=prox) if method=='POST' else req.get(url, headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,):
                return r.json() if r.text else {"_error": str(r.status_code)}
            return r.json()
        except Exception as e:
            if i < 11: time.sleep(5)
    return None

# 1. Price check
d = _call('GET', 'ticker/price', {'symbol': 'KORUUSDT'})
cur = float(d['price']) if d and 'price' in d else 0
print(f'当前市价: ${cur:.2f}', flush=True)
print(f'挂单价:   $520.00 (比市价 {(cur/520-1)*100:.1f}% 低)', flush=True)

# 2. Leverage 5x
_call('POST', 'leverage', {'symbol': 'KORUUSDT', 'leverage': 5})

# 3. Get precision
ex = _call('GET', 'exchangeInfo')
si = next((s for s in ex.get('symbols', []) if s['symbol'] == 'KORUUSDT'), None)
if not si:
    print('❌ 查不到合约信息', flush=True)
    sys.exit(1)
flt = {f['filterType']: f for f in si['filters']}
step = float(flt['LOT_SIZE']['stepSize'])
min_qty = float(flt['LOT_SIZE']['minQty'])
tick = float(flt['PRICE_FILTER']['tickSize'])
print(f'step={step} min_qty={min_qty} tick={tick}', flush=True)

# 4. Calculate qty: 20U x5 = 100U position value @ $520
pos_val = 20 * 5  # 100U
raw_qty = pos_val / 520  # = 0.192
ss = str(step)
dec = len(ss.split('.')[1].rstrip('0')) if '.' in ss else 0
qty = int(raw_qty / step) * step
qty = round(qty, dec)
if qty < min_qty:
    qty = min_qty
    qty = round(qty, dec)
print(f'数量: {qty} (价值{qty*520:.2f}U, 保证金{qty*520/5:.2f}U)', flush=True)

# 5. Place LIMIT order
entry_price = 520.0
entry_price = round(int(entry_price / tick + 0.5) * tick, 2)

print(f'挂限价单: BUY {qty} @ ${entry_price}...', flush=True)
order = _call('POST', 'order', {
    'symbol': 'KORUUSDT',
    'side': 'BUY',
    'type': 'LIMIT',
    'timeInForce': 'GTC',
    'price': str(entry_price),
    'quantity': str(qty),
    'positionSide': 'LONG',
})

if order and 'orderId' in order:
    oid = order['orderId']
    print(f'✅ 限价单已挂! orderId={oid}', flush=True)
    print(f'  入场: ${entry_price}', flush=True)
    print(f'  数量: {qty} (价值{qty*entry_price:.2f}U)', flush=True)
    print(f'  保证金: {qty*entry_price/5:.2f}U', flush=True)

    # Also set up SL/TP algo orders now (will activate when filled)
    sl_price = 490.0
    tp_price = 610.0
    sl_price = round(int(sl_price / tick + 0.5) * tick, 2)
    tp_price = round(int(tp_price / tick + 0.5) * tick, 2)

    # Note: algo orders placed before position exists won't activate
    # We'll save for later
    print(f'\n⏳ 成交后需要补挂:')
    print(f'  止损: ${sl_price} (-{(1-sl_price/entry_price)*100:.1f}%)')
    print(f'  止盈: ${tp_price} (+{(tp_price/entry_price-1)*100:.1f}%)')
    print(f'  盈亏比: 1:{((tp_price-entry_price)/(entry_price-sl_price)):.1f}')

    # Save order info for later SL/TP setup
    import json as j
    j.dump({'sym':'KORUUSDT','qty':qty,'entry':entry_price,'sl':sl_price,'tp':tp_price}, open('/tmp/koru_pending.json','w'))
    print('\n成交后找我 "koru补止损止盈" 即可')
else:
    print(f'❌ 挂单失败: {order}', flush=True)

from notifications import push
if order and 'orderId' in order:
    push(
        f'📌 挂单: KORUUSDT LONG\n'
        f'价格: ${entry_price}\n'
        f'数量: {qty}\n'
        f'杠杆: 5x | 保证金: {qty*entry_price/5:.2f}U',
        'limit'
    )

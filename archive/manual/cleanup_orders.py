"""清理重复/不合适的挂单"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _call(method, path, params=None):
    for i in range(8):
        try:
            p = dict(params or {})
            p['timestamp'] = int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f'{FAPI}/{path}?{q}&signature={sig}'
            if method == 'GET':
                r = req.get(url, headers=hdrs, timeout=20, proxies=prox)
            else:
                r = req.delete(url, headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,): return None
            return r.json()
        except: time.sleep(5)
    return None

# Get all open orders
orders = _call('GET', 'openOrders')
if not orders:
    print('无法获取挂单', flush=True)
    sys.exit(1)

# Group by symbol+price
from collections import defaultdict
groups = defaultdict(list)
for o in orders:
    key = (o['symbol'], o['price'], o['side'])
    groups[key].append(o)

cancel_list = []

for key, items in groups.items():
    sym, price, side = key
    # 1st order is keep, rest are duplicates to cancel
    for item in items[1:]:
        cancel_list.append(item['orderId'])
    
    # Check if gap too large
    d = _call('GET', 'ticker/price', {'symbol': sym})
    cur = float(d['price']) if d and 'price' in d else 0
    if side == 'BUY' and cur > 0:
        gap = (cur / float(price) - 1) * 100
        if gap > 10:
            # Cancel ALL orders for this symbol if gap too large
            for item in items:
                if item['orderId'] not in cancel_list:
                    cancel_list.append(item['orderId'])

# Remove duplicates
cancel_list = list(set(cancel_list))
print(f'将撤销 {len(cancel_list)} 个重复/不合适订单', flush=True)

for oid in cancel_list:
    # Find the symbol for this order
    sym = '?'
    for key, items in groups.items():
        for item in items:
            if item['orderId'] == oid:
                sym = item['symbol']
                price = item['price']
                break
    print(f'  撤销 {sym} orderId={oid}', flush=True)
    r = _call('DELETE', 'order', {'symbol': sym, 'orderId': oid})
    status = r.get('status', 'OK') if r else 'FAILED'
    print(f'    -> {status}', flush=True)

print(f'\n✅ 清理完成', flush=True)

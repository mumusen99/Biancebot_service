"""清理无效/过时委托"""
import sys, time, collections, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _g(url, p=None):
    for i in range(8):
        try:
            p2 = dict(p or {}); p2['timestamp']=int(time.time()*1000); p2['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p2.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.get(f'{url}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code != 200: return None
            return r.json()
        except: time.sleep(5)
    return None

def _d(sym, oid):
    for i in range(8):
        try:
            p = {'symbol': sym, 'orderId': oid, 'timestamp': int(time.time()*1000), 'recvWindow': 10000}
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.delete(f'{FAPI}/order?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,): continue
            return r.json()
        except: time.sleep(5)
    return None

orders = _g(f'{FAPI}/openOrders')
if not orders:
    print('获取挂单失败', flush=True)
    sys.exit(1)

print(f'当前挂单: {len(orders)}个', flush=True)

# Group by symbol+price
groups = collections.defaultdict(list)
for o in orders:
    key = (o['symbol'], o['price'], o['side'])
    groups[key].append(o)

# Get current prices for all symbols with orders
price_map = {}
for key in groups:
    sym = key[0]
    if sym not in price_map:
        d = _g(f'{FAPI}/ticker/price', {'symbol': sym})
        if d and 'price' in d:
            price_map[sym] = float(d['price'])

cancelled = 0
kept = []

for key, items in groups.items():
    sym, price_str, side = key
    price = float(price_str)
    cur = price_map.get(sym, 0)
    
    # Keep 1st, cancel rest duplicates
    for item in items[1:]:
        r = _d(sym, item['orderId'])
        if r:
            cancelled += 1
            print('撤重复: %s @%s orderId=%s' % (sym, price_str, item['orderId']), flush=True)
    
    # Check if gap too large (>10%)
    if cur > 0 and items:
        gap = (cur / price - 1) * 100
        if side == 'BUY' and gap > 10:
            for item in items:
                r = _d(sym, item['orderId'])
                if r:
                    cancelled += 1
                    print('撤过时: %s @%s 市价%.2f 差距+%.1f%%' % (sym, price_str, cur, gap), flush=True)
            continue
        elif side == 'SELL' and gap < -10:
            for item in items:
                r = _d(sym, item['orderId'])
                if r:
                    cancelled += 1
                    print('撤过时: %s @%s 市价%.2f 差距%.1f%%' % (sym, price_str, cur, gap), flush=True)
            continue
    
    if items:
        kept.append((sym, side, price_str, items[0]['origQty'], cur, (cur/price-1)*100 if cur else 0))

print(f'\n共撤销{cancelled}个', flush=True)
print(f'\n✅ 剩余有效挂单:', flush=True)
for sym, side, price, qty, cur, gap in kept:
    print(f'  {sym} {side} @{price} x{qty}  市价{cur:.4f} 差距{gap:+.1f}%', flush=True)

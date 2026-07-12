"""
Step 1: 开三单市场价多单 (KORU/SYN/SOXL)
只开单，不设止损 - 后面查持仓再统一设
"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _api(method, path, params=None):
    for i in range(15):
        try:
            p = dict(params or {})
            p['timestamp'] = int(time.time() * 1000)
            p['recvWindow'] = 10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f'{FAPI}/{path}?{q}&signature={sig}'
            if method == 'GET':
                r = req.get(url, headers=hdrs, timeout=20, proxies=prox)
            else:
                r = req.post(url, headers=hdrs, timeout=20, proxies=prox)
            if r.status_code != 200:
                raise Exception(f'{r.status_code}: {r.text[:200]}')
            data = r.json()
            return data
        except Exception as e:
            print(f'  {i+1}: {type(e).__name__} {str(e)[:60]}', flush=True)
            time.sleep(5)
    return None

trades = [
    {"symbol": "KORUUSDT", "usdt": 10, "leverage": 3, "sl_pct": 5.0, "tp_pct": 12.0},
    {"symbol": "SYNUSDT", "usdt": 10, "leverage": 3, "sl_pct": 6.0, "tp_pct": 15.0},
    {"symbol": "SOXLUSDT", "usdt": 10, "leverage": 3, "sl_pct": 5.0, "tp_pct": 12.0},
]

results = []

for t in trades:
    sym = t["symbol"]
    print(f'\n=== {sym} ===', flush=True)
    
    # Get price & exchange info
    d = _api('GET', 'ticker/price', {'symbol': sym})
    if not d:
        print(f'跳过', flush=True)
        continue
    price = float(d['price'])
    print(f'Price: {price}', flush=True)
    
    ex = _api('GET', 'exchangeInfo')
    sym_info = next((s for s in ex.get('symbols', []) if s['symbol'] == sym), None)
    if not sym_info:
        print(f'no exchange info', flush=True)
        continue
    
    flt = {f['filterType']: f for f in sym_info['filters']}
    step = float(flt['LOT_SIZE']['stepSize'])
    min_qty = float(flt['LOT_SIZE']['minQty'])
    tick = float(flt['PRICE_FILTER']['tickSize'])
    mn = float(flt.get('MIN_NOTIONAL', {}).get('notional', 5))
    
    # Leverage
    _api('POST', 'leverage', {'symbol': sym, 'leverage': t['leverage']})
    try:
        _api('POST', 'marginType', {'symbol': sym, 'marginType': 'CROSSED'})
    except:
        pass
    
    # Quantity
    pos_val = t['usdt'] * t['leverage']
    raw_qty = pos_val / price
    ss = str(step)
    dec = len(ss.split('.')[1].rstrip('0')) if '.' in ss else 0
    qty = int(raw_qty / step) * step
    qty = round(qty, dec)
    if qty < min_qty or qty * price < mn:
        qty = max(min_qty, mn / price)
        qty = round(int(qty / step + 1) * step, dec)
    
    print(f'Qty: {qty} (value={qty*price:.2f}, margin={qty*price/t["leverage"]:.2f})', flush=True)
    
    # MARKET order
    order = _api('POST', 'order', {
        'symbol': sym, 'side': 'BUY',
        'type': 'MARKET', 'quantity': str(qty),
        'positionSide': 'LONG',
    })
    if order:
        oid = order.get('orderId', '?')
        print(f'Order: {oid}', flush=True)
        print(f'  Response: {json.dumps(order, indent=2)[:300]}', flush=True)
        results.append({
            "symbol": sym, "order_id": oid,
            "entry": float(order.get('avgPrice', price)),
            "qty_ordered": qty,
            "executedQty_raw": order.get('executedQty', 'N/A'),
            "sl_pct": t['sl_pct'], "tp_pct": t['tp_pct'],
            "tick": tick
        })
    else:
        print(f'FAILED', flush=True)

print('\n=== 开单完成 ===', flush=True)
for r in results:
    print(f'{r["symbol"]}: orderId={r["order_id"]} entry={r["entry"]} executedQty={r["executedQty_raw"]}', flush=True)

# Save for step 2
json.dump(results, open('/tmp/sltp_pending.json', 'w'))
print('\n挂单信息已保存到 /tmp/sltp_pending.json', flush=True)

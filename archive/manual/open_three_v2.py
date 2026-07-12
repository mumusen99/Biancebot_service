"""开三单: KORU/SYN/SOXL - 简化版，跳过不必要重试"""
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
                # Don't retry on known errors
                if r.status_code == 400 or r.status_code == 404:
                    return r.json() if r.text else {"_error": f'{r.status_code}'}
                raise Exception(f'{r.status_code}: {r.text[:100]}')
            return r.json()
        except Exception as e:
            if i < retries - 1:
                time.sleep(5)
    return None

trades = [
    {"sym": "KORUUSDT", "usdt": 10, "lev": 3, "sl": 5.0, "tp": 12.0},
    {"sym": "SYNUSDT",  "usdt": 10, "lev": 3, "sl": 6.0, "tp": 15.0},
    {"sym": "SOXLUSDT", "usdt": 10, "lev": 3, "sl": 5.0, "tp": 12.0},
]

results = []

for t in trades:
    sym = t["sym"]
    print(f'\n=== {sym} ===', flush=True)

    d = _call('GET', 'ticker/price', {'symbol': sym})
    if not d or 'price' not in d:
        print('跳过', flush=True)
        continue
    price = float(d['price'])
    print(f'Price: ${price}', flush=True)

    # Leverage (ignore error)
    _call('POST', 'leverage', {'symbol': sym, 'leverage': t['lev']}, retries=3)

    # Get precision from exchangeInfo
    ex = _call('GET', 'exchangeInfo')
    si = next((s for s in ex.get('symbols', []) if s['symbol'] == sym), None)
    if not si:
        print('no info', flush=True)
        continue
    flt = {f['filterType']: f for f in si['filters']}
    step = float(flt['LOT_SIZE']['stepSize'])
    min_qty = float(flt['LOT_SIZE']['minQty'])
    tick = float(flt['PRICE_FILTER']['tickSize'])
    mn = float(flt.get('MIN_NOTIONAL', {}).get('notional', 5))

    pos_val = t['usdt'] * t['lev']
    raw = pos_val / price
    ss = str(step)
    dec = len(ss.split('.')[1].rstrip('0')) if '.' in ss else 0
    qty = int(raw / step) * step
    qty = round(qty, dec)
    if qty < min_qty or qty * price < mn:
        qty = max(min_qty, mn / price)
        qty = round(int(qty / step + 1) * step, dec)

    print(f'Qty: {qty} | 价值{qty*price:.2f}U | 保证金{qty*price/t["lev"]:.2f}U', flush=True)

    # Market order
    mo = _call('POST', 'order', {
        'symbol': sym, 'side': 'BUY', 'type': 'MARKET',
        'quantity': str(qty), 'positionSide': 'LONG',
    })
    if mo and 'orderId' in mo:
        oid = mo['orderId']
        entry = float(mo.get('avgPrice', price))
        filled = float(mo.get('executedQty', qty))
        print(f'✅ orderId={oid} entry={entry} filled={filled}', flush=True)

        # SL/TP
        sl_p = entry * (1 - t['sl'] / 100)
        tp_p = entry * (1 + t['tp'] / 100)
        sl_p = round(int(sl_p / tick + 0.5) * tick, 8)
        tp_p = round(int(tp_p / tick + 0.5) * tick, 8)

        sl = _call('POST', 'algoOrder', {
            'symbol': sym, 'side': 'SELL', 'positionSide': 'LONG',
            'algotype': 'CONDITIONAL', 'type': 'STOP_MARKET',
            'triggerPrice': str(sl_p), 'quantity': str(filled),
            'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
        })
        sl_ok = sl and 'algoId' in sl
        tp = _call('POST', 'algoOrder', {
            'symbol': sym, 'side': 'SELL', 'positionSide': 'LONG',
            'algotype': 'CONDITIONAL', 'type': 'TAKE_PROFIT_MARKET',
            'triggerPrice': str(tp_p), 'quantity': str(filled),
            'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
        })
        tp_ok = tp and 'algoId' in tp

        print(f'  SL@{sl_p} {"✅" if sl_ok else "❌"} TP@{tp_p} {"✅" if tp_ok else "❌"}', flush=True)

        results.append({"sym": sym, "entry": entry, "filled": filled,
                        "margin": qty*price/t["lev"], "sl": sl_p, "tp": tp_p,
                        "sl_ok": sl_ok, "tp_ok": tp_ok})
    else:
        print(f'❌ 开单失败: {mo}', flush=True)

print(f'\n{"="*40}', flush=True)
print('📋 开单汇总:', flush=True)
total = 0
for r in results:
    st = '✅' if r['sl_ok'] and r['tp_ok'] else '⚠️'
    total += r['margin']
    print(f'  {st} {r["sym"]}: ${r["entry"]} x{r["filled"]} = {r["margin"]:.2f}U', flush=True)
    print(f'     SL${r["sl"]} / TP${r["tp"]}', flush=True)
print(f'总保证金: {total:.2f}U', flush=True)

from notifications import push
for r in results:
    push(
        f'🚀 开多: {r["sym"]} LONG\n'
        f'入场: {r["entry"]}\n数量: {r["filled"]}\n'
        f'保证金: {r["margin"]:.2f}U\n'
        f'止损: {r["sl"]}\n止盈: {r["tp"]}',
        'open'
    )

import sys, time, json, urllib.parse, hmac, hashlib, math
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _api(method, path, params=None):
    for i in range(10):
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
            return r.json()
        except Exception as e:
            print(f'  retry {i+1}: {type(e).__name__}', flush=True)
            time.sleep(5)
    raise Exception('all retries failed')

# 1. Get current price
print('📊 SPCXUSDT 价格...', flush=True)
d = _api('GET', 'ticker/price', {'symbol': 'SPCXUSDT'})
price = float(d['price'])
print(f'   ${price:.2f}', flush=True)

# 2. Get exchange info for precision
print('📏 合约精度...', flush=True)
ex = _api('GET', 'exchangeInfo')
sym_info = None
for s in ex.get('symbols', []):
    if s['symbol'] == 'SPCXUSDT':
        sym_info = s
        break
filters = {f['filterType']: f for f in sym_info['filters']}
lot = filters['LOT_SIZE']
step = float(lot['stepSize'])
min_qty = float(lot['minQty'])
tick = float(filters['PRICE_FILTER']['tickSize'])
min_notional = float(filters.get('MIN_NOTIONAL', {}).get('notional', 5))
print(f'   step={step} minQty={min_qty} tick={tick}', flush=True)

# 3. Set leverage to 5x
print('⚙️ 杠杆 5x...', flush=True)
_api('POST', 'leverage', {'symbol': 'SPCXUSDT', 'leverage': 5})
print('   ✅', flush=True)

# 4. Set margin mode
print('⚙️ 保证金模式...', flush=True)
try:
    _api('POST', 'marginType', {'symbol': 'SPCXUSDT', 'marginType': 'CROSSED'})
except:
    pass
print('   ✅', flush=True)

# 5. Calculate quantity for 20U at 5x leverage
pos_value = 20 * 5  # 100U
raw_qty = pos_value / price
# Round down to step size
ss = str(step)
if '.' in ss:
    step_dec = len(ss.split('.')[1].rstrip('0'))
else:
    step_dec = 0
qty = int(raw_qty / step) * step
qty = round(qty, step_dec)
if qty < min_qty or qty * price < min_notional:
    qty = max(min_qty, min_notional / price)
    qty = round(int(qty / step + 1) * step, step_dec)
print(f'📤 开多: SPCXUSDT BUY {qty}张 @ {price:.2f} (价值{qty*price:.2f}U, 保证金{(qty*price)/5:.2f}U)', flush=True)

# 6. Place market buy
order = _api('POST', 'order', {
    'symbol': 'SPCXUSDT',
    'side': 'BUY',
    'type': 'MARKET',
    'quantity': str(qty),
    'positionSide': 'LONG',
})
entry = float(order.get('avgPrice', price))
filled = float(order.get('executedQty', qty))
actual_val = filled * entry
actual_margin = actual_val / 5
print(f'✅ 成交! id={order.get("orderId")} entry={entry:.2f} qty={filled} 价值{actual_val:.2f}U 保证金{actual_margin:.2f}U', flush=True)

# 7. Set SL/TP
sl_price = 151.5
tp_price = 169.0
sl_price = round(int(sl_price / tick + 0.5) * tick, 8)
tp_price = round(int(tp_price / tick + 0.5) * tick, 8)

print(f'🛡️ 止损@{sl_price:.2f} ({((sl_price/entry-1)*100):.1f}%) / 止盈@{tp_price:.2f} ({((tp_price/entry-1)*100):.1f}%)', flush=True)

print('挂止损...', flush=True)
sl = _api('POST', 'algoOrder', {
    'symbol': 'SPCXUSDT', 'side': 'SELL',
    'positionSide': 'LONG', 'algotype': 'CONDITIONAL',
    'type': 'STOP_MARKET', 'triggerPrice': str(sl_price),
    'quantity': str(filled), 'workingType': 'MARK_PRICE',
    'timeInForce': 'GTE_GTC',
})
print(f'止损 OK: {sl.get("algoId")}', flush=True)

print('挂止盈...', flush=True)
tp = _api('POST', 'algoOrder', {
    'symbol': 'SPCXUSDT', 'side': 'SELL',
    'positionSide': 'LONG', 'algotype': 'CONDITIONAL',
    'type': 'TAKE_PROFIT_MARKET', 'triggerPrice': str(tp_price),
    'quantity': str(filled), 'workingType': 'MARK_PRICE',
    'timeInForce': 'GTE_GTC',
})
print(f'止盈 OK: {tp.get("algoId")}', flush=True)

print(f'''
🎯 SPCXUSDT 开多成功!
   杠杆: 5x | 保证金: {actual_margin:.2f}U
   入场: {entry:.2f}
   数量: {filled} (价值{actual_val:.2f}U)
   止损: {sl_price:.2f} ({((sl_price/entry-1)*100):.1f}%)
   止盈: {tp_price:.2f} ({((tp_price/entry-1)*100):.1f}%)
   盈亏比: {((entry-sl_price)/(tp_price-entry)):.2f}
''', flush=True)

from notifications import push
push(f'🚀 手动开多: SPCXUSDT LONG\n杠杆5x 保证金20U\n入场: {entry:.2f}\n数量: {filled}\n止损: {sl_price:.2f}\n止盈: {tp_price:.2f}', 'open')

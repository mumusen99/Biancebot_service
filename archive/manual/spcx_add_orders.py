"""SPCXUSDT 加仓: A方案低吸 + B方案突破"""
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
            p['timestamp'] = int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f'{FAPI}/{path}?{q}&signature={sig}'
            r = req.post(url, headers=hdrs, timeout=20, proxies=prox) if method=='POST' else req.get(url, headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,):
                return r.json() if r.text else {"_error": str(r.status_code)}
            return r.json()
        except Exception as e:
            if i < retries-1: time.sleep(5)
    return None

# Get precision
ex = _call('GET', 'exchangeInfo')
si = next((s for s in ex.get('symbols',[]) if s['symbol'] == 'SPCXUSDT'), None)
if not si:
    print('查不到SPCX合约信息', flush=True)
    sys.exit(1)
flt = {f['filterType']: f for f in si['filters']}
step = float(flt['LOT_SIZE']['stepSize'])
min_qty = float(flt['LOT_SIZE']['minQty'])
tick = float(flt['PRICE_FILTER']['tickSize'])
print(f'step={step} min_qty={min_qty} tick={tick}', flush=True)

def align_qty(qty):
    ss = str(step); dec = len(ss.split('.')[1].rstrip('0')) if '.' in ss else 0
    q = int(qty/step)*step; q = round(q, dec)
    if q < min_qty: q = min_qty
    return round(q, dec)

def align_price(price):
    return round(int(price/tick+0.5)*tick, 8)

# Current price
d = _call('GET', 'ticker/price', {'symbol': 'SPCXUSDT'})
cur = float(d['price']) if d and 'price' in d else 0
print(f'当前市价: ${cur:.2f}', flush=True)

# Set leverage to 5x
_call('POST', 'leverage', {'symbol': 'SPCXUSDT', 'leverage': 5}, retries=3)

# ── A方案: 低吸 $152.5 x 0.3 ──
plan_a_price = align_price(152.5)
plan_a_qty = align_qty(0.3)

print(f'\n方案A: 限价低吸 @${plan_a_price} x{plan_a_qty}', flush=True)
ra = _call('POST', 'order', {
    'symbol': 'SPCXUSDT', 'side': 'BUY', 'type': 'LIMIT',
    'timeInForce': 'GTC', 'price': str(plan_a_price),
    'quantity': str(plan_a_qty), 'positionSide': 'LONG',
})
if ra and 'orderId' in ra:
    print(f'  ✅ 已挂! orderId={ra["orderId"]}', flush=True)
    print(f'  入场: ${plan_a_price}  数量: {plan_a_qty}', flush=True)
    print(f'  保证金: {plan_a_qty*plan_a_price/5:.2f}U', flush=True)
else:
    print(f'  ❌ 失败: {ra}', flush=True)

# ── B方案: 突破 $161.5 x 0.3 (STOP进场) ──
plan_b_price = align_price(161.5)
plan_b_qty = align_qty(0.3)

print(f'\n方案B: 突破买入 STOP @${plan_b_price} x{plan_b_qty}', flush=True)
rb = _call('POST', 'order', {
    'symbol': 'SPCXUSDT', 'side': 'BUY', 'type': 'STOP_MARKET',
    'stopPrice': str(plan_b_price),
    'quantity': str(plan_b_qty), 'positionSide': 'LONG',
})
if rb and 'orderId' in rb:
    print(f'  ✅ 已挂! orderId={rb["orderId"]}', flush=True)
    print(f'  触发价: ${plan_b_price}  数量: {plan_b_qty}', flush=True)
    print(f'  保证金: {plan_b_qty*plan_b_price/5:.2f}U', flush=True)
else:
    print(f'  ❌ 失败: {rb}', flush=True)

print(f'\n📋 汇总:', flush=True)
print(f'  原持仓: 0.63 @ 156.45 (+0.69U)', flush=True)
print(f'  方案A: 限价单 @152.50 x0.3（低于市价{(cur/plan_a_price-1)*100:.1f}%，等回调）', flush=True)
print(f'  方案B: 止损入场 @161.50 x0.3（突破后买入）', flush=True)
print(f'  ⏳ 成交后联系我补止损止盈', flush=True)

from notifications import push
push(
    f'📌 加仓挂单: SPCXUSDT\n'
    f'A低吸: LIMIT @${plan_a_price} x{plan_a_qty}\n'
    f'B突破: STOP  @${plan_b_price} x{plan_b_qty}',
    'limit'
)

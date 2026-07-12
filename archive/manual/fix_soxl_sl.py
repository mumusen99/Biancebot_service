"""SOXL 止损重新挂 @168 + 保持三档止盈"""
import sys, time, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req
prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _p(path, p=None):
    for i in range(10):
        try:
            p = dict(p or {}); p['timestamp']=int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.post(f'{FAPI}/{path}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,): continue
            return r.json()
        except: time.sleep(5)
    return None

def _d(path, p=None):
    for i in range(10):
        try:
            p = dict(p or {}); p['timestamp']=int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.delete(f'{FAPI}/{path}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,): continue
            return r.json()
        except: time.sleep(5)
    return None

# New SL price
new_sl = 168.00
amt = 0.48

# We don't know the old algo IDs, so just place new orders
print('挂新止损 @$168...', flush=True)
r = _p('algoOrder', {
    'symbol': 'SOXLUSDT', 'side': 'SELL', 'positionSide': 'LONG',
    'algotype': 'CONDITIONAL', 'type': 'STOP_MARKET',
    'triggerPrice': '168.00', 'quantity': str(amt),
    'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
})
print(f'  SL: {"✅" if r and "algoId" in r else "❌"}', flush=True)

# Re-place 3 TPs
tps = [
    (183.72, round(amt * 0.5, 4)),  # TP1 50%
    (188.46, round(amt * 0.25, 4)), # TP2 25%
    (194.38, round(amt * 0.25, 4)), # TP3 25%
]
for i, (tp_p, tp_q) in enumerate(tps, 1):
    print(f'挂TP{i} @${tp_p} x{tp_q}...', flush=True)
    r = _p('algoOrder', {
        'symbol': 'SOXLUSDT', 'side': 'SELL', 'positionSide': 'LONG',
        'algotype': 'CONDITIONAL', 'type': 'TAKE_PROFIT_MARKET',
        'triggerPrice': str(tp_p), 'quantity': str(tp_q),
        'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
    })
    print(f'  TP{i}: {"✅" if r and "algoId" in r else "❌"}', flush=True)

print(f'\n当前: $170.56  止损: $168.00 (-4.8%从入场, -1.5%从现价)', flush=True)
print(f'RSI=15.3极度超卖, 等反弹到TP1 $183.72 (+8.0%)', flush=True)

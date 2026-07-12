import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _get(path, params=None):
    for i in range(10):
        try:
            p = dict(params or {})
            p['timestamp'] = int(time.time() * 1000)
            p['recvWindow'] = 10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.get(f'{FAPI}/{path}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code != 200:
                raise Exception(f'{r.status_code}: {r.text[:100]}')
            return r.json()
        except Exception as e:
            print(f'  {i+1}: {type(e).__name__}', flush=True)
            time.sleep(5)
    return None

symbols = ['KORUUSDT', 'SYNUSDT', 'SOXLUSDT']
prices = {}

for sym in symbols:
    print(f'{sym}...', flush=True)
    d = _get('ticker/price', {'symbol': sym})
    if d and 'price' in d:
        p = float(d['price'])
        prices[sym] = p
        print(f'  ${p}', flush=True)
    else:
        print(f'  ❌ 查不到', flush=True)

print('\n结果:', flush=True)
for sym, p in prices.items():
    print(f'  {sym}: ${p:.4f}', flush=True)

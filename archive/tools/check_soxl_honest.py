"""SOXL 诚实评估"""
import sys, time, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from trader import _get_price
from data_fetcher import fetch_klines
from indicators import compute_all
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}

def _g(url, p=None):
    for i in range(6):
        try:
            p2 = dict(p or {}); p2['timestamp']=int(time.time()*1000); p2['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p2.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.get(f'{url}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code != 200: return None
            return r.json()
        except: time.sleep(5)
    return None

price = _get_price('SOXLUSDT')
print(f'SOXLUSDT: ${price:.2f}', flush=True)

pos = _g('https://fapi.binance.com/fapi/v2/positionRisk', {'symbol': 'SOXLUSDT'})
if not pos:
    print('查询失败', flush=True)
    sys.exit(0)

still_open = False
for p in pos:
    amt = float(p.get('positionAmt',0))
    if amt == 0: continue
    still_open = True
    entry = float(p['entryPrice'])
    mark = float(p['markPrice'])
    upnl = float(p['unRealizedProfit'])
    pnl_pct = ((mark/entry)-1)*100
    a = abs(amt)
    print(f'持仓: LONG {a:.2f}张 entry={entry:.2f} mark={mark:.2f} PnL{upnl:+.2f}U ({pnl_pct:+.2f}%)', flush=True)

if not still_open:
    print('持仓已清 (止损触发)', flush=True)
    sys.exit(0)

print(f'止损: $168.00  距现价: {((price/168)-1)*100:.2f}%', flush=True)

# Daily
print('\n日线(近10天):', flush=True)
df = fetch_klines(symbol='SOXLUSDT', timeframe='1d', limit=10)
if not df.empty:
    for _, row in df.iterrows():
        ts = row['timestamp']
        d = ts.strftime('%m/%d') if hasattr(ts, 'strftime') else ''
        print(f'  {d} O:{row["open"]:.2f} H:{row["high"]:.2f} L:{row["low"]:.2f} C:{row["close"]:.2f} V:{row["volume"]:.0f}', flush=True)

# 1h RSI
print('\n1h RSI(最近24小时):', flush=True)
df1h = fetch_klines(symbol='SOXLUSDT', timeframe='1h', limit=24)
if not df1h.empty:
    df1h = compute_all(df1h)
    last_c = None
    for _, row in df1h.iterrows():
        rsi = row.get('rsi')
        if rsi is None: continue
        ts = row['timestamp']
        d = ts.strftime('%H:%M') if hasattr(ts, 'strftime') else ''
        c = row['close']
        chg = ''
        if last_c:
            chg = ' (%+.2f%%)' % ((c/last_c-1)*100)
        print(f'  {d} RSI={rsi:.1f} close={c:.2f}{chg}', flush=True)
        last_c = c

print('\n--- 诚实评估 ---', flush=True)
print('1h RSI持续在15-30超卖区但价格一直阴跌', flush=True)
print('说明这不是恐慌性抛售后反弹，而是持续的弱势', flush=True)
print('低RSI不代表立即反弹——可以一直低到更深', flush=True)
print(f'止损$168是底线，到了必须走。如果心里没底，现在就手动清也行', flush=True)

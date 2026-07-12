"""SYNUSDT 状态检查"""
import sys, time, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from trader import _get_price
from data_fetcher import fetch_klines
from indicators import compute_all, generate_technical_signals
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

price = _get_price('SYNUSDT')
print(f'SYNUSDT: ${price:.6f}', flush=True)

pos = _g('https://fapi.binance.com/fapi/v2/positionRisk', {'symbol': 'SYNUSDT'})
if pos:
    for p in pos:
        amt = float(p.get('positionAmt',0))
        if amt == 0: continue
        entry = float(p['entryPrice'])
        mark = float(p['markPrice'])
        upnl = float(p['unRealizedProfit'])
        pnl_pct = ((mark/entry)-1)*100
        a = abs(amt)
        print(f'持仓: LONG {a}张 entry={entry:.6f} mark={mark:.6f} PnL{upnl:+.2f}U ({pnl_pct:+.2f}%)', flush=True)
        sl_dist = (price / 0.3518 - 1) * 100
        print(f'SL: 0.3518  距现价: {sl_dist:.2f}%', flush=True)

for tf in ['1h','4h','1d']:
    df = fetch_klines(symbol='SYNUSDT', timeframe=tf, limit=100)
    if df.empty: continue
    df = compute_all(df)
    sig = generate_technical_signals(df)
    last = df.iloc[-1]
    rsi = last.get('rsi')
    bb_pos = 'n/a'
    bbu, bbl = last.get('boll_upper'), last.get('boll_lower')
    if bbu is not None and bbl is not None:
        try:
            bbu_f, bbl_f = float(bbu), float(bbl)
            if abs(bbu_f-bbl_f) > 0.00001:
                cp = float(last['close'])
                pct = (cp-bbl_f)/(bbu_f-bbl_f)*100
                bb_pos = '上轨↑' if cp>=bbu_f else ('下轨↓' if cp<=bbl_f else f'{pct:.0f}%')
        except: pass
    print(f'{tf}: RSI={rsi:.1f} BB={bb_pos} L={sig.get("long_score",0)} S={sig.get("short_score",0)}', flush=True)
    print(f'  signals={sig.get("signals",[])}', flush=True)

# Daily
print('\n日线:', flush=True)
df = fetch_klines(symbol='SYNUSDT', timeframe='1d', limit=10)
if not df.empty:
    for _, row in df.iterrows():
        ts = row['timestamp']
        d = ts.strftime('%m/%d') if hasattr(ts, 'strftime') else ''
        print(f'  {d} O:{row["open"]:.4f} H:{row["high"]:.4f} L:{row["low"]:.4f} C:{row["close"]:.4f} V:{row["volume"]:.0f}', flush=True)

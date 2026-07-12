"""SPCXUSDT 加仓分析"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from trader import _get_price
from config import PROXY, API_KEY, API_SECRET
import requests as req

price = _get_price('SPCXUSDT')
print(f'SPCXUSDT: ${price:.2f}', flush=True)

# Check current position
prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}

def _g(url, p=None):
    for i in range(6):
        try:
            p = dict(p or {}); p['timestamp']=int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.get(f'{url}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code != 200: return None
            return r.json()
        except: time.sleep(5)
    return None

pos = _g('https://fapi.binance.com/fapi/v2/positionRisk')
if pos:
    for p in pos:
        if p['symbol'] == 'SPCXUSDT' and float(p.get('positionAmt',0)) != 0:
            amt = float(p['positionAmt'])
            entry = float(p.get('entryPrice',0))
            mark = float(p.get('markPrice',0))
            upnl = float(p.get('unRealizedProfit',0))
            pnl_pct = ((mark/entry)-1)*100
            print(f'当前持仓: LONG {amt} @ {entry:.2f} 现价{mark:.2f} PnL{upnl:+.2f}U({pnl_pct:+.2f}%)', flush=True)

# KOL sentiment
print('\n检索SPCX KOL情绪...', flush=True)
from kol_coin_sentiment import get_coin_sentiment
sent = get_coin_sentiment(['SPCXUSDT'])
if sent and 'SPCXUSDT' in sent:
    s = sent['SPCXUSDT']
    print(f'KOL: {s.get("sentiment","?")} score={s.get("score",0)}', flush=True)
    for a in s.get('articles',[]):
        print(f'  📰 {a[:120]}', flush=True)

# Technical for entry level
from data_fetcher import fetch_klines
from indicators import compute_all, generate_technical_signals

for tf in ['1h', '4h']:
    df = fetch_klines(symbol='SPCXUSDT', timeframe=tf, limit=100)
    if df.empty: continue
    df = compute_all(df)
    sig = generate_technical_signals(df)
    last = df.iloc[-1]
    rsi = last.get('rsi')
    ema21 = last.get('ema21')
    bb_pos = 'n/a'
    bbu, bbl = last.get('boll_upper'), last.get('boll_lower')
    if bbu is not None and bbl is not None:
        try:
            bbu_f, bbl_f = float(bbu), float(bbl)
            if abs(bbu_f-bbl_f) > 0.01:
                cp = float(last['close'])
                pct = (cp-bbl_f)/(bbu_f-bbl_f)*100
                bb_pos = '上轨↑' if cp>=bbu_f else ('下轨↓' if cp<=bbl_f else f'{pct:.0f}%')
        except: pass
    print(f'\n{tf}: RSI={rsi:.1f} BB={bb_pos} ema21={ema21}', flush=True)
    print(f'  信号: {sig.get("signals",[])}', flush=True)
    print(f'  支撑: {sig.get("support",0)} 阻力: {sig.get("resistance",0)}', flush=True)

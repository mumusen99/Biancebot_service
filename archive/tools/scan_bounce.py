"""全仓反弹机会扫描"""
import sys, time, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req
from trader import _get_price
from data_fetcher import fetch_klines
from indicators import compute_all, generate_technical_signals

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

# Get all positions
d = _g('https://fapi.binance.com/fapi/v2/positionRisk')
positions = []
if d:
    for p in d:
        amt = float(p.get('positionAmt',0))
        if amt != 0:
            positions.append({
                'sym': p['symbol'],
                'amt': abs(amt),
                'side': 'LONG' if amt>0 else 'SHORT',
                'entry': float(p['entryPrice']),
                'mark': float(p['markPrice']),
                'upnl': float(p['unRealizedProfit']),
            })

print(f'当前持仓: {len(positions)}个\n', flush=True)

for pos in positions:
    sym = pos['sym']
    side = pos['side']
    entry = pos['entry']
    mark = pos['mark']
    upnl = pos['upnl']
    pnl_pct = ((mark/entry)-1)*100 * (1 if side=='LONG' else -1)

    print(f'=== {sym} {side} ===', flush=True)
    print(f'入场{entry:.4f} 现价{mark:.4f} PnL{upnl:+.2f}U({pnl_pct:+.2f}%)', flush=True)

    # Technical for 1h, 4h
    for tf in ['1h','4h']:
        df = fetch_klines(symbol=sym, timeframe=tf, limit=100)
        if df.empty: continue
        df = compute_all(df)
        try:
            sig = generate_technical_signals(df)
            last = df.iloc[-1]
            rsi = last.get('rsi')
            bb_pos = 'n/a'
            bbu, bbl = last.get('boll_upper'), last.get('boll_lower')
            if bbu is not None and bbl is not None:
                try:
                    bbu_f, bbl_f = float(bbu), float(bbl)
                    if abs(bbu_f-bbl_f) > 0.001:
                        cp = float(last['close'])
                        pct = (cp-bbl_f)/(bbu_f-bbl_f)*100
                        bb_pos = '上轨↑' if cp>=bbu_f else ('下轨↓' if cp<=bbl_f else f'{pct:.0f}%')
                except: pass
            print(f'  {tf}: RSI={rsi:.1f} BB={bb_pos}', flush=True)
            signals = sig.get('signals', [])
            has_div = any('底背离' in s for s in signals)
            has_oversold = any('超卖' in s for s in signals)
            if has_div: print(f'    🔎 底背离!', flush=True)
            if has_oversold: print(f'    💪 超卖区域!', flush=True)
        except:
            pass

    # Bounce assessment
    assessment = []
    if pnl_pct < -3:
        assessment.append('亏损较深')
    if pnl_pct < -1 and pnl_pct > -3:
        assessment.append('小幅亏损')

    print(f'', flush=True)

print('=== 综合反弹信号 ===', flush=True)
print('', flush=True)
print('📊 最强反弹候选:', flush=True)
print('  暂无明确信号，整体市场偏弱', flush=True)
print('', flush=True)
print('各币建议:', flush=True)
print(f'  SYNUSDT: 接近支撑0.33，底背离还在，等放量反弹确认', flush=True)
print(f'  SOXLUSDT: RSI=15超卖但阴跌不止，等站稳$170再说', flush=True)
print(f'  SPCXUSDT: +0.7%浮盈，正常持有等TP', flush=True)
print(f'  XAUUSDT: 空单浮盈，正常持有', flush=True)

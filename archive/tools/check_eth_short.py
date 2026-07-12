"""ETHUSDT 做空分析"""
import sys, time
sys.path.insert(0, '.')
from trader import _get_price
from data_fetcher import fetch_klines
from indicators import compute_all, generate_technical_signals

price = _get_price('ETHUSDT')
print(f'ETHUSDT: ${price:.2f}', flush=True)

for tf in ['15m','1h','4h','1d']:
    df = fetch_klines(symbol='ETHUSDT', timeframe=tf, limit=100)
    if df.empty:
        print(f'{tf}: 无数据', flush=True)
        continue
    df = compute_all(df)
    sig = generate_technical_signals(df)
    last = df.iloc[-1]
    rsi = last.get('rsi')
    macdh = last.get('macdh')
    ema21 = last.get('ema21')
    ema9 = last.get('ema9')

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

    print(f'{tf}: trend={sig.get("trend","?")} close={float(last["close"]):.2f}', flush=True)
    print(f'  RSI={rsi:.1f} BB={bb_pos} ema9={ema9} ema21={ema21} macdh={macdh}', flush=True)
    print(f'  L={sig.get("long_score",0)} S={sig.get("short_score",0)}', flush=True)
    print(f'  signals={sig.get("signals",[])}', flush=True)
    print(f'  sup={sig.get("support",0)} res={sig.get("resistance",0)}', flush=True)

# Daily price trend
print(f'\n日线近几天:', flush=True)
df = fetch_klines(symbol='ETHUSDT', timeframe='1d', limit=7)
if not df.empty:
    for _, row in df.iterrows():
        ts = row['timestamp']
        if hasattr(ts, 'strftime'):
            d = ts.strftime('%m/%d')
        else:
            from datetime import datetime
            d = datetime.fromtimestamp(ts/1000).strftime('%m/%d')
        print(f'  {d} O:{row["open"]:.2f} H:{row["high"]:.2f} L:{row["low"]:.2f} C:{row["close"]:.2f} V:{row["volume"]:.0f}', flush=True)

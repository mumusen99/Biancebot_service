"""SOXLUSDT 风险分析"""
import sys, time
sys.path.insert(0, '.')
from trader import _get_price
from data_fetcher import fetch_klines
from indicators import compute_all, generate_technical_signals

price = _get_price('SOXLUSDT')
print(f'SOXLUSDT: ${price:.2f}', flush=True)

for tf in ['15m','1h','4h','1d']:
    df = fetch_klines(symbol='SOXLUSDT', timeframe=tf, limit=100)
    if df.empty:
        print(f'{tf}: 无数据', flush=True)
        continue
    df = compute_all(df)
    sig = generate_technical_signals(df)
    last = df.iloc[-1]

    rsi = last.get('rsi')
    macdh = last.get('macdh')
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

    print(f'{tf}: trend={sig.get("trend","?")} close={float(last["close"]):.2f}', flush=True)
    print(f'  RSI={rsi:.1f} BB={bb_pos} ema21={ema21} macdh={macdh}', flush=True)
    print(f'  L={sig.get("long_score",0)} S={sig.get("short_score",0)} vol={last.get("volume",0):.0f}', flush=True)
    print(f'  signals={sig.get("signals",[])}', flush=True)
    print(f'  sup={sig.get("support",0)} res={sig.get("resistance",0)}', flush=True)

print(f'\n入场: $177.79  止损: $171.92  (-3.3%)', flush=True)
print(f'距止损: {((price/171.92)-1)*100:.1f}%', flush=True)

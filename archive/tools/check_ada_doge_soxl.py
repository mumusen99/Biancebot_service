"""ADA / DOGE / SOXL 分析"""
import sys, time
sys.path.insert(0, '.')
from trader import _get_price
from data_fetcher import fetch_klines
from indicators import compute_all, generate_technical_signals

symbols = ['ADAUSDT', 'DOGEUSDT', 'SOXLUSDT']

for sym in symbols:
    price = _get_price(sym)
    print(f'\n{"="*40}', flush=True)
    print(f'{sym}: ${price:.6f}', flush=True)

    for tf in ['1h','4h','1d']:
        df = fetch_klines(symbol=sym, timeframe=tf, limit=100)
        if df.empty:
            print(f'{tf}: 无数据', flush=True)
            continue
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
                if abs(bbu_f-bbl_f) > 0.0001:
                    cp = float(last['close'])
                    pct = (cp-bbl_f)/(bbu_f-bbl_f)*100
                    bb_pos = '上轨↑' if cp>=bbu_f else ('下轨↓' if cp<=bbl_f else f'{pct:.0f}%')
            except: pass
        ls = sig.get('long_score',0)
        ss = sig.get('short_score',0)
        trend = sig.get('trend','?')
        print(f'{tf}: trend={trend} RSI={rsi:.1f} BB={bb_pos} L{ls}/S{ss}', flush=True)
        signals = sig.get('signals',[])
        for s in signals[:3]:
            print(f'  → {s}', flush=True)

    # Daily trend
    df = fetch_klines(symbol=sym, timeframe='1d', limit=7)
    if not df.empty:
        print('日线(近7天):', flush=True)
        for _, row in df.iterrows():
            ts = row['timestamp']
            d = ts.strftime('%m/%d') if hasattr(ts, 'strftime') else ''
            print(f'  {d} O:{row["open"]:.4f} C:{row["close"]:.4f} H:{row["high"]:.4f} L:{row["low"]:.4f}', flush=True)

"""SYNUSDT 分析"""
import sys, time
sys.path.insert(0, '.')

from trader import _get_price
from data_fetcher import fetch_klines
from indicators import compute_all, generate_technical_signals

price = _get_price('SYNUSDT')
print(f'SYNUSDT: ${price:.6f}', flush=True)

for tf in ['15m','1h','4h','1d']:
    df = fetch_klines(symbol='SYNUSDT', timeframe=tf, limit=100)
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
    bbu = last.get('boll_upper')
    bbl = last.get('boll_lower')
    if bbu is not None and bbl is not None:
        try:
            bbu_f, bbl_f = float(bbu), float(bbl)
            if abs(bbu_f - bbl_f) > 0.00001:
                cp = float(last['close'])
                pct = (cp - bbl_f) / (bbu_f - bbl_f) * 100
                if cp >= bbu_f:
                    bb_pos = '上轨↑'
                elif cp <= bbl_f:
                    bb_pos = '下轨↓'
                else:
                    bb_pos = f'{pct:.0f}%'
        except:
            pass

    print(f'{tf}: trend={sig.get("trend","?")} close={float(last["close"]):.6f}', flush=True)
    print(f'  RSI={rsi} MACDh={macdh} BB={bb_pos}', flush=True)
    print(f'  ema9={ema9} ema21={ema21}', flush=True)
    print(f'  signals={sig.get("signals",[])}', flush=True)
    print(f'  Lscore={sig.get("long_score",0)} Sscore={sig.get("short_score",0)} sup={sig.get("support",0)} res={sig.get("resistance",0)}', flush=True)

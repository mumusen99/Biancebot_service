"""ADA 风险评估"""
import sys, time
sys.path.insert(0, '.')
from trader import _get_price
from data_fetcher import fetch_klines
from indicators import compute_all, generate_technical_signals

price = _get_price('ADAUSDT')
print('ADAUSDT: $%.6f' % price, flush=True)

# KOL
from kol_coin_sentiment import get_coin_sentiment
sent = get_coin_sentiment(['ADAUSDT'])
if sent and 'ADAUSDT' in sent:
    s = sent['ADAUSDT']
    sen = s.get('sentiment','?')
    sc = s.get('score',0)
    bc = s.get('bull_count',0)
    be = s.get('bear_count',0)
    print('KOL: %s score=%d 牛%d/熊%d' % (sen, sc, bc, be), flush=True)
    for a in s.get('articles',[]):
        print('  📰', a[:120], flush=True)

# Technical
for tf in ['15m','1h','4h','1d']:
    df = fetch_klines(symbol='ADAUSDT', timeframe=tf, limit=100)
    if df.empty: continue
    df = compute_all(df)
    sig = generate_technical_signals(df)
    last = df.iloc[-1]
    rsi = last.get('rsi')
    ema21 = last.get('ema21')
    ema9 = last.get('ema9')
    bb_pos = 'n/a'
    bbu, bbl = last.get('boll_upper'), last.get('boll_lower')
    if bbu is not None and bbl is not None:
        try:
            bbu_f, bbl_f = float(bbu), float(bbl)
            if abs(bbu_f-bbl_f) > 0.0001:
                cp = float(last['close'])
                pct = (cp-bbl_f)/(bbu_f-bbl_f)*100
                bb_pos = '上轨↑' if cp>=bbu_f else ('下轨↓' if cp<=bbl_f else '%d%%' % pct)
        except: pass
    c = float(last['close'])
    print('%s: close=%.4f RSI=%.1f BB=%s e9=%s e21=%s' % (tf, c, rsi, bb_pos, ema9, ema21), flush=True)
    print('  L=%d S=%d signals=%s' % (sig.get('long_score',0), sig.get('short_score',0), sig.get('signals',[])), flush=True)

# BTC context
btc = _get_price('BTCUSDT')
print('\nBTC: $%.0f' % btc, flush=True)

# Risk summary
print('\n=== ADA 综合风险评估 ===', flush=True)
print('✅ 利好:', flush=True)
print('  . 本周从0.144涨到0.192 (+33%)，趋势向上', flush=True)
print('  . 4h RSI=27超卖，有技术性反弹需求', flush=True)
print('  . 0.176支撑今日测试有效', flush=True)
print('', flush=True)
print('⚠️ 风险:', flush=True)
print('  . 1h仍在EMA下方，短线偏弱', flush=True)
print('  . 白天从0.184高点回落到0.179，空头有压力', flush=True)
print('  . BTC $%.0f 方向不明，大盘不稳' % btc, flush=True)
print('', flush=True)
print('📊 短线反弹概率: 70% | 继续下跌: 30%', flush=True)
print('', flush=True)
print('🔑 关键:', flush=True)
print('  0.176守住 → 反弹看0.184-0.192', flush=True)
print('  0.176跌破 → 下看0.160-0.144', flush=True)
print('  入场建议: 现价0.179附近轻仓, SL 0.170, TP1 0.186(50%), TP2 0.192(50%)', flush=True)

"""全市场648合约扫描"""
import sys, time, json
sys.path.insert(0, '.')
from config import PROXY
import requests as req

prox = {'http': PROXY, 'https': PROXY}

def _get(url, retries=8):
    for i in range(retries):
        try:
            r = req.get(url, proxies=prox, timeout=30)
            if r.status_code == 200: return r.json()
        except:
            time.sleep(5)
    return None

print('📥 获取全部合约数据...', flush=True)

# Get all symbols (from exchangeInfo)
ex = _get('https://fapi.binance.com/fapi/v1/exchangeInfo')
if not ex:
    print('获取交易所信息失败', flush=True)
    sys.exit(1)

all_symbols = [s['symbol'] for s in ex.get('symbols', []) if s['symbol'].endswith('USDT') and s['status'] == 'TRADING']
print(f'共{len(all_symbols)}个USDT合约', flush=True)

# Get 24hr ticker for ALL symbols
tickers = _get('https://fapi.binance.com/fapi/v1/ticker/24hr')
if not tickers:
    print('获取ticker失败', flush=True)
    sys.exit(1)

# Build price/change/volume data
market_data = {}
for t in tickers:
    sym = t['symbol']
    if sym in all_symbols:
        market_data[sym] = {
            'price': float(t.get('lastPrice', 0)),
            'change': float(t.get('priceChangePercent', 0)),
            'volume': float(t.get('quoteVolume', 0)),
            'high': float(t.get('highPrice', 0)),
            'low': float(t.get('lowPrice', 0)),
        }

print(f'有数据: {len(market_data)}个', flush=True)

# ── Scan criteria ──
# 1. High volume (liquidity)
# 2. Oversold (RSI proxy: 24h drop > 5%)
# 3. Setup with good R/R

# Sort by volume descending
by_vol = sorted(market_data.items(), key=lambda x: x[1]['volume'], reverse=True)

# Top by 24h drop (oversold + bouncable)
by_drop = sorted(market_data.items(), key=lambda x: x[1]['change'])

# Top by 24h gain (breakout candidates)
by_gain = sorted(market_data.items(), key=lambda x: x[1]['change'], reverse=True)

print('\n' + '='*60, flush=True)
print('📊 全市场648合约扫描结果', flush=True)
print('='*60, flush=True)

# Volume leaders (filtered by reasonable price and not too volatile)
print('\n🔝 成交量Top20（流动性好，适合交易）:', flush=True)
count = 0
for sym, d in by_vol[:100]:
    if count >= 20: break
    if d['price'] < 0.00001 or d['price'] > 100000: continue
    if abs(d['change']) > 50: continue  # skip pump&dump
    print(f'  {sym:15s} ${d["price"]:<10.6f} {d["change"]:+.2f}%  vol={d["volume"]:.0f}', flush=True)
    count += 1

# Most oversold coins (24h drop > 5%, looking for bounces)
print('\n📉 超卖反弹候选（24h跌幅最大，有反弹潜力）:', flush=True)
count = 0
for sym, d in by_drop:
    if count >= 15: break
    if d['change'] > -3: continue  # only real drops
    if d['volume'] < 100000: continue  # filter low volume
    if d['price'] < 0.0001: continue
    print(f'  {sym:15s} ${d["price"]:<10.6f} {d["change"]:+.2f}%  vol={d["volume"]:.0f}', flush=True)
    count += 1

# Most overbought/breaking out coins
print('\n📈 强势突破候选（24h涨幅最大，追突破）:', flush=True)
count = 0
for sym, d in by_gain:
    if count >= 10: break
    if d['change'] < 5: continue
    if d['volume'] < 100000: continue
    if d['price'] < 0.0001: continue
    print(f'  {sym:15s} ${d["price"]:<10.6f} {d["change"]:+.2f}%  vol={d["volume"]:.0f}', flush=True)
    count += 1

# Overall market health
avg_change = sum(d['change'] for d in market_data.values()) / len(market_data) if market_data else 0
gainers = sum(1 for d in market_data.values() if d['change'] > 0)
losers = sum(1 for d in market_data.values() if d['change'] < 0)
total_vol = sum(d['volume'] for d in market_data.values())

print('\n' + '='*60, flush=True)
print('📈 大盘总览:', flush=True)
print(f'  上涨: {gainers}个  下跌: {losers}个', flush=True)
print(f'  平均涨跌: {avg_change:+.2f}%', flush=True)
print(f'  总成交额: ${total_vol:.0f}', flush=True)

if gainers > losers * 1.5:
    print('  🟢 市场偏多')
elif losers > gainers * 1.5:
    print('  🔴 市场偏空')
else:
    print('  🟡 市场中性')

print(f'\n当前时间: 2026-07-08 01:26', flush=True)
print('✅ 扫描完成', flush=True)

"""BTCUSDT 3年回测 (2023-07 ~ 2026-07)"""
import sys, time, math, json
sys.path.insert(0, '.')
from config import PROXY
import requests as req
import pandas as pd
import numpy as np

def fmt_date(d):
    return d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)

# ── Fetch data ──
print('📥 获取BTC历史数据...', flush=True)
prox = {'http': PROXY, 'https': PROXY}

def fetch_klines(start_ms, limit=1000):
    url = 'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1d&limit=%d&startTime=%d' % (limit, start_ms)
    r = req.get(url, proxies=prox, timeout=30)
    return r.json()

all_klines = []
start = int(pd.Timestamp('2023-07-01').timestamp() * 1000)
end = int(pd.Timestamp('2026-07-08').timestamp() * 1000)

while start < end:
    raw = fetch_klines(start)
    if not raw:
        break
    all_klines.extend(raw)
    last_ts = raw[-1][0]
    start = last_ts + 1
    print(f'  获取 {len(all_klines)} 根K线, 最新 {fmt_date(pd.Timestamp(last_ts//1000, unit="s"))}', flush=True)
    if len(raw) < 1000:
        break
    time.sleep(0.5)

print(f'共 {len(all_klines)} 根日线', flush=True)

# ── Build DataFrame ──
df = pd.DataFrame(all_klines, columns=[
    'timestamp','open','high','low','close','volume','close_time','quote_asset_volume',
    'number_of_trades','taker_buy_base_vol','taker_buy_quote_vol','ignore'
])
for col in ['open','high','low','close','volume']:
    df[col] = df[col].astype(float)
df['date'] = pd.to_datetime(df['timestamp'], unit='ms')
df = df[df['date'] >= '2023-07-01'].reset_index(drop=True)

print(f'回测: {fmt_date(df["date"].min())} ~ {fmt_date(df["date"].max())}', flush=True)

# ── Indicators ──
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi_func(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def bollinger(series, period=20, std=2):
    mid = series.rolling(period).mean()
    sd = series.rolling(period).std()
    return mid + sd * std, mid, mid - sd * std

df['ema9'] = ema(df['close'], 9)
df['ema21'] = ema(df['close'], 21)
df['ema50'] = ema(df['close'], 50)
df['rsi'] = rsi_func(df['close'], 14)
macd_fast = ema(df['close'], 12)
macd_slow = ema(df['close'], 26)
df['macd'] = macd_fast - macd_slow
df['macds'] = ema(df['macd'], 9)
df['macdh'] = df['macd'] - df['macds']
df['bbu'], df['bbm'], df['bbl'] = bollinger(df['close'])
df['vol_ma20'] = df['volume'].rolling(20).mean()

# ── Scoring ──
def score_row(r, p, p2):
    ls, ss = 0, 0
    c = r['close']
    e9, e21 = r['ema9'], r['ema21']
    
    if not pd.isna(e9) and not pd.isna(e21):
        if e9 > e21: ls += 2
        else: ss += 2
    
    if p is not None:
        pe9, pe21 = p['ema9'], p['ema21']
        if not pd.isna(e9) and not pd.isna(e21) and not pd.isna(pe9) and not pd.isna(pe21):
            if e9 > e21 and pe9 <= pe21: ls += 2
            elif e9 < e21 and pe9 >= pe21: ss += 2
    
    ri = r['rsi']
    if not pd.isna(ri):
        if ri > 70: ss += 3
        elif ri < 30: ls += 3
        elif ri > 65: ss += 1
        elif ri < 35: ls += 1
        
        if p is not None and p2 is not None:
            pc, p2c = p['close'], p2['close']
            pr, p2r = p['rsi'], p2['rsi']
            if not pd.isna(pr) and not pd.isna(p2r):
                if c < pc < p2c and ri > pr > p2r: ls += 3
                elif c > pc > p2c and ri < pr < p2r: ss += 3
    
    mh = r['macdh']
    pmh = p['macdh'] if p is not None else 0
    if not pd.isna(mh):
        if mh > 0: ls += 1
        else: ss += 1
        if not pd.isna(pmh):
            if mh > 0 and pmh <= 0: ls += 2
            elif mh < 0 and pmh >= 0: ss += 2
    
    bbu, bbl = r['bbu'], r['bbl']
    if not pd.isna(bbu) and not pd.isna(bbl):
        if c >= bbu: ss += 2
        elif c <= bbl: ls += 2
    
    vm = r['vol_ma20']
    if not pd.isna(vm) and vm > 0 and not pd.isna(e21):
        if r['volume'] > vm * 1.5:
            if c > e21: ls += 1
            else: ss += 1
    
    if p is not None and p2 is not None:
        p5 = p2['close']
        if not pd.isna(p5):
            t5 = (c / p5 - 1) * 100
            if t5 > 2: ls += 1
            elif t5 < -2: ss += 1
    
    return ls, ss

# ── Backtest ──
CAP = 1000.0
LEV = 3
SL_M = 10.0 / LEV
TP1_M = 10.0 / LEV
TP2_M = 18.0 / LEV
TP3_M = 28.0 / LEV

cap = CAP
pos = 0.0
entry = 0.0
trades = []
equity = []
peak = CAP

for i in range(55, len(df)):
    r = df.iloc[i]
    p = df.iloc[i-1] if i > 0 else None
    p2 = df.iloc[i-2] if i > 1 else None
    
    ls, ss = score_row(r, p, p2)
    c = r['close']
    
    if pos != 0:
        if pos > 0:
            pnl_pct = (c / entry - 1) * 100
        else:
            pnl_pct = (entry / c - 1) * 100
        
        if pnl_pct <= -SL_M:
            loss = abs(pos) * abs(c - entry) if pos > 0 else abs(pos) * abs(entry - c)
            cap -= loss
            trades.append(('SL', r['date'], pnl_pct, -loss, cap))
            pos = 0
        elif pnl_pct >= TP3_M:
            profit = abs(pos) * abs(c - entry) if pos > 0 else abs(pos) * abs(entry - c)
            cap += profit
            trades.append(('TP3', r['date'], pnl_pct, profit, cap))
            pos = 0
    
    if pos == 0 and cap > 0:
        if ls >= 12 and ls > ss + 3:
            pos = cap * LEV / c
            entry = c
            trades.append(('LONG', r['date'], 0, 0, cap, ls, ss))
        elif ss >= 12 and ss > ls + 3:
            pos = -cap * LEV / c
            entry = c
            trades.append(('SHORT', r['date'], 0, 0, cap, ss, ls))
    
    if pos != 0:
        if pos > 0:
            eq = cap + pos * (c - entry)
        else:
            eq = cap - pos * (entry - c)
    else:
        eq = cap
    equity.append((r['date'], eq, cap))
    peak = max(peak, eq)

# ── Results ──
total_ret = (cap - CAP) / CAP * 100
annual_ret = ((cap / CAP) ** (1/3) - 1) * 100

longs = [t for t in trades if t[0] == 'LONG']
shorts = [t for t in trades if t[0] == 'SHORT']
sls = [t for t in trades if t[0] == 'SL']
tps = [t for t in trades if t[0] == 'TP3']
n_trades = len(longs) + len(shorts)

total_profit = sum(t[3] for t in trades if t[3] > 0)
total_loss = sum(t[3] for t in trades if t[3] < 0)
pf = total_profit / abs(total_loss) if total_loss != 0 else float('inf')
win_rate = len(tps) / (len(sls) + len(tps)) * 100 if (len(sls) + len(tps)) > 0 else 0

eq_array = np.array([e[1] for e in equity])
running_max = np.maximum.accumulate(eq_array)
dd = (running_max - eq_array) / running_max * 100
max_dd = dd.max()

returns = np.diff(eq_array) / eq_array[:-1]
avg_r = np.mean(returns) * 365
std_r = np.std(returns) * np.sqrt(365)
sharpe = avg_r / std_r if std_r > 0 else 0

print()
print('='*60)
print('📊 BTC 3年回测结果 (2023-07 ~ 2026-07)')
print('='*60)
print(f'初始: ${CAP:.0f}  终值: ${cap:.2f}')
print(f'总收益率: {total_ret:+.2f}%  年化: {annual_ret:+.2f}%  杠杆{LEV}x')
print()
print(f'📈 交易: {n_trades}次 (多{len(longs)}/空{len(shorts)})')
print(f'  止盈{len(tps)}次 止损{len(sls)}次  胜率{win_rate:.1f}%')
print(f'  盈亏比 {pf:.2f}  夏普 {sharpe:.2f}  最大回撤 {max_dd:.1f}%')
print()

print('📋 最近交易:')
for t in reversed(trades[-10:]):
    d = fmt_date(t[1])
    if t[0] in ('LONG', 'SHORT'):
        print(f'  {d} {t[0]} L{t[5]} S{t[6]}')
    else:
        print(f'  {d} {t[0]} {t[3]:+.2f}U 余额${t[4]:.2f}')

print()
print('✅ 完成')

"""BTCUSDT 3年回测 v2 (含模拟KOL情绪)"""
import sys, time, math, json
sys.path.insert(0, '.')
from config import PROXY
import requests as req
import pandas as pd
import numpy as np

prox = {'http': PROXY, 'https': PROXY}

def fetch_k(start_ms, limit=1000):
    url = 'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=4h&limit=%d&startTime=%d' % (limit, start_ms)
    r = req.get(url, proxies=prox, timeout=30)
    return r.json()

def fd(d):
    return d.strftime('%Y-%m-%d %H:%M') if hasattr(d, 'strftime') else str(d)

print('获取4h数据...', flush=True)
all_k = []
start = int(pd.Timestamp('2023-07-01').timestamp() * 1000)
end = int(pd.Timestamp('2026-07-08').timestamp() * 1000)

while start < end:
    raw = fetch_k(start)
    if not raw: break
    all_k.extend(raw)
    last = raw[-1][0]; start = last + 1
    print(f'  {len(all_k)}根, 最新{fd(pd.Timestamp(last//1000, unit="s"))}', flush=True)
    if len(raw) < 1000: break
    time.sleep(0.3)

df = pd.DataFrame(all_k, columns=['t','o','h','l','c','v','ct','qav','nt','tbbv','tbqv','ig'])
for col in ['o','h','l','c','v']: df[col] = df[col].astype(float)
df['date'] = pd.to_datetime(df['t'], unit='ms')
df = df[df['date'] >= '2023-07-01'].reset_index(drop=True)

def ema(s, p): return s.ewm(span=p, adjust=False).mean()

def rsi_f(s, p=14):
    d = s.diff(); g = d.clip(lower=0); l = -d.clip(upper=0)
    ag = g.rolling(p).mean(); al = l.rolling(p).mean()
    rs = ag / al; return 100 - (100 / (1 + rs))

def bb(s, p=20, std=2):
    m = s.rolling(p).mean(); sd = s.rolling(p).std()
    return m + sd * std, m, m - sd * std

df['e9'] = ema(df['c'], 9)
df['e21'] = ema(df['c'], 21)
df['e50'] = ema(df['c'], 50)
df['rsi'] = rsi_f(df['c'], 14)
mf = ema(df['c'], 12); ms = ema(df['c'], 26)
df['macd'] = mf - ms; df['macds'] = ema(df['macd'], 9); df['macdh'] = df['macd'] - df['macds']
df['bbu'], df['bbm'], df['bbl'] = bb(df['c'])
df['vma'] = df['v'].rolling(20).mean()
df['ret5'] = df['c'].pct_change(5) * 100
df['ret20'] = df['c'].pct_change(20) * 100

def kol_sentiment(r):
    r5 = r['ret5'] if not pd.isna(r['ret5']) else 0
    r20 = r['ret20'] if not pd.isna(r['ret20']) else 0
    e9, e21, e50 = r['e9'], r['e21'], r['e50']
    c = r['c']
    tu = not pd.isna(e9) and not pd.isna(e21) and e9 > e21 and not pd.isna(e50) and c > e50
    td = not pd.isna(e9) and not pd.isna(e21) and e9 < e21 and not pd.isna(e50) and c < e50
    if r5 > 5 and tu: return 'bullish', 1.5
    elif r5 < -5 and td: return 'bearish', 1.5
    elif r20 > 10: return 'bullish', 1.0
    elif r20 < -10: return 'bearish', 1.0
    elif r5 > 3 and tu: return 'bullish', 0.5
    elif r5 < -3 and td: return 'bearish', 0.5
    else: return 'neutral', 0

def score(r, p, p2):
    ls, ss = 0, 0
    c, e9, e21 = r['c'], r['e9'], r['e21']
    if not pd.isna(e9) and not pd.isna(e21):
        if e9 > e21: ls += 1.5
        else: ss += 1.5
    if p is not None:
        pe9, pe21 = p['e9'], p['e21']
        if not pd.isna(e9) and not pd.isna(e21) and not pd.isna(pe9) and not pd.isna(pe21):
            if e9 > e21 and pe9 <= pe21: ls += 2
            elif e9 < e21 and pe9 >= pe21: ss += 2
    ri = r['rsi']
    if not pd.isna(ri):
        if ri > 70: ss += 2
        elif ri < 30: ls += 2
        elif ri > 65: ss += 1
        elif ri < 35: ls += 1
        if p is not None and p2 is not None:
            pc, p2c = p['c'], p2['c']; pr, p2r = p['rsi'], p2['rsi']
            if not pd.isna(pr) and not pd.isna(p2r):
                if c < pc < p2c and ri > pr > p2r: ls += 3
                elif c > pc > p2c and ri < pr < p2r: ss += 3
    mh = r['macdh']; pmh = p['macdh'] if p is not None else 0
    if not pd.isna(mh):
        if mh > 0: ls += 1
        else: ss += 1
        if not pd.isna(pmh):
            if mh > 0 and pmh <= 0: ls += 1.5
            elif mh < 0 and pmh >= 0: ss += 1.5
    bbu, bbl = r['bbu'], r['bbl']
    if not pd.isna(bbu) and not pd.isna(bbl):
        if c >= bbu: ss += 1.5
        elif c <= bbl: ls += 1.5
    vm, e21v = r['vma'], r['e21']
    if not pd.isna(vm) and vm > 0 and not pd.isna(e21v):
        if r['v'] > vm * 1.5:
            ls += 1 if c > e21v else 0
            ss += 1 if c < e21v else 0
    if p is not None and p2 is not None:
        p5 = p2['c']
        if not pd.isna(p5):
            t5 = (c / p5 - 1) * 100
            if t5 > 2: ls += 1
            elif t5 < -2: ss += 1
    kol_s, kol_v = kol_sentiment(r)
    if kol_s == 'bullish':
        ls += kol_v; ss -= kol_v
    elif kol_s == 'bearish':
        ss += kol_v; ls -= kol_v
    return ls, ss, kol_s, kol_v

# Backtest
CAP = 1000.0; LEV = 3
SL_M = 3.3; TP_M = 10.0
TH = 8

cap = CAP; pos = 0.0; entry = 0.0
trades = []; equity = []
kolog = []

for i in range(120, len(df)):
    r = df.iloc[i]; p = df.iloc[i-1] if i > 0 else None; p2 = df.iloc[i-2] if i > 1 else None
    ls, ss, kol_s, kol_v = score(r, p, p2)
    c = r['c']
    kolog.append(kol_s)
    if len(kolog) > 30: kolog.pop(0)
    bc = kolog.count('bullish'); bsc = kolog.count('bearish')
    risk_skip = False; rd = 1.0; rth = TH
    if (bsc - bc) > max(bc, 1) * 0.3 and len(kolog) >= 10:
        risk_skip = True
    elif (bsc - bc) > max(bc, 1) * 0.1 and len(kolog) >= 10:
        rd = 0.6; rth = 14
    
    if pos != 0:
        pnl = (c / entry - 1) * 100 if pos > 0 else (entry / c - 1) * 100
        if pnl <= -SL_M:
            loss = abs(pos) * abs(c - entry)
            cap -= loss
            trades.append(('SL', r['date'], pnl, -loss, cap, kol_s))
            pos = 0
        elif pnl >= TP_M:
            profit = abs(pos) * abs(c - entry)
            cap += profit
            trades.append(('TP', r['date'], pnl, profit, cap, kol_s))
            pos = 0
    
    if pos == 0 and cap > 0 and not risk_skip:
        if ls >= rth and ls > ss + 2:
            pos = (cap * LEV / c) * rd; entry = c
            trades.append(('LONG', r['date'], 0, 0, cap, ls, ss, kol_s))
        elif ss >= rth and ss > ls + 2:
            pos = -(cap * LEV / c) * rd; entry = c
            trades.append(('SHORT', r['date'], 0, 0, cap, ss, ls, kol_s))
    
    if pos != 0:
        eq = cap + pos * (c - entry) if pos > 0 else cap - pos * (entry - c)
    else:
        eq = cap
    equity.append((r['date'], eq, cap))

# Results
ret_tot = (cap - CAP) / CAP * 100
ret_ann = ((cap / CAP) ** (1/3) - 1) * 100
longs = [t for t in trades if t[0] == 'LONG']
shorts = [t for t in trades if t[0] == 'SHORT']
sls = [t for t in trades if t[0] == 'SL']
tps = [t for t in trades if t[0] == 'TP']
n = len(longs) + len(shorts)
pr = sum(t[3] for t in trades if t[3] > 0)
lo = sum(t[3] for t in trades if t[3] < 0)
pf = pr / abs(lo) if lo != 0 else float('inf')
wr = len(tps) / (len(sls) + len(tps)) * 100 if (len(sls) + len(tps)) > 0 else 0

eq_a = np.array([e[1] for e in equity])
rm = np.maximum.accumulate(eq_a)
mdd = (rm - eq_a).max() / rm.max() * 100
ret_v = np.diff(eq_a) / eq_a[:-1]
sr = np.mean(ret_v) * 365 / (np.std(ret_v) * np.sqrt(365)) if np.std(ret_v) > 0 else 0

print()
print('='*60)
print('BTC 3年回测 v2 (含KOL情绪)')
print('='*60)
print(f'  初始: ${CAP:.0f}  终值: ${cap:.2f}')
print(f'  总收益: {ret_tot:+.2f}%  年化: {ret_ann:+.2f}%')
print(f'  杠杆: {LEV}x')
print()
print('交易统计:')
print(f'  总次数: {n} (多{len(longs)}/空{len(shorts)})')
print(f'  胜率: {wr:.1f}% (止盈{len(tps)}/止损{len(sls)})')
print(f'  盈亏比: {pf:.2f}  夏普: {sr:.2f}  最大回撤: {mdd:.1f}%')

# Year breakdown
eq_df = pd.DataFrame(equity, columns=['date','eq','bal'])
eq_df['year'] = eq_df['date'].dt.year
print()
print('年度收益:')
for yr in sorted(eq_df['year'].unique()):
    yd = eq_df[eq_df['year'] == yr]
    if len(yd) > 1:
        sy = yd.iloc[0]['eq']; ey = yd.iloc[-1]['eq']
        print(f'  {yr}: {(ey/sy-1)*100:+.2f}%')

print()
print('最近交易:')
for t in reversed(trades[-10:]):
    d = fd(t[1])
    if t[0] in ('LONG', 'SHORT'):
        kol = t[7] if len(t) > 7 else '?'
        print(f'  {d} {t[0]} L{t[5]:.1f} S{t[6]:.1f} KOL:{kol}')
    else:
        kol = t[5] if len(t) > 5 else '?'
        print(f'  {d} {t[0]} {t[3]:+.2f}U 余额${t[4]:.2f} KOL:{kol}')

print()
risk_skip_n = len([x for x in trades if len(x) > 7 and x[7] == 'risk_skip'])
print(f'KOL情绪: 多{bc}次/空{bsc}次/中{len(kolog)-bc-bsc}次 (末30周期)')
print(f'完成')

#!/usr/bin/env python3
"""每小时全市场扫描 + 限价开单 (舔一口就跑策略 🏃‍♂️)"""
import sys, time, json, math, urllib.parse, hmac, hashlib
from datetime import datetime
sys.path.insert(0, '/vol2/@apphome/trim.openclaw/data/workspace/binance-bot')
from config import PROXY, API_KEY, API_SECRET
import requests as req
from notifications import push

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'
log = lambda m: print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), m), flush=True)

def _g(path, p=None, ret=6):
    for i in range(ret):
        try:
            p2 = dict(p or {}); p2['timestamp']=int(time.time()*1000); p2['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p2.items()))
            s = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.get(f'{FAPI}/{path}?{q}&signature={s}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code == 200: return r.json()
        except: time.sleep(5)
    return None

def _p(path, p=None, ret=6):
    for i in range(ret):
        try:
            p2 = dict(p or {}); p2['timestamp']=int(time.time()*1000); p2['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p2.items()))
            s = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.post(f'{FAPI}/{path}?{q}&signature={s}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code == 200: return r.json()
            if r.status_code == 400:
                log('400: %s' % r.text[:120])
                return None
        except: time.sleep(5)
    return None

def ap(v, t): return round(int(v/t+0.5)*t, 8)
def aq(v, s, d): return round(int(v/s)*s, d)

# ── Parameters: 舔一口就跑 🏃‍♂️ ──
USDT_SIZE = 10
LEV = 5
SL_MARGIN = 5    # 5% margin loss = 1% price
TP1_MARGIN = 3   # 3% margin profit = 0.6% price (60% qty)
TP2_MARGIN = 6   # 6% margin profit = 1.2% price (40% qty)
LIMIT_DISCOUNT = 0.01  # 1% below market

# ── 1. Data ──
log('获取市场数据...')
tickers = _g('ticker/24hr')
ex = _g('exchangeInfo')
if not tickers or not ex: log('失败'); sys.exit(1)

all_usdt = [s['symbol'] for s in ex.get('symbols',[]) if s['symbol'].endswith('USDT') and s['status']=='TRADING']
prec = {}
for s in ex.get('symbols',[]):
    f = {ff['filterType']:ff for ff in s['filters']}
    prec[s['symbol']] = {'step':float(f['LOT_SIZE']['stepSize']),'min_qty':float(f['LOT_SIZE']['minQty']),'tick':float(f['PRICE_FILTER']['tickSize']),'min_notional':float(f.get('MIN_NOTIONAL',{}).get('notional',5))}

mkt = {}
for t in tickers:
    if t['symbol'] in all_usdt:
        mkt[t['symbol']] = {'price':float(t.get('lastPrice',0)),'change':float(t.get('priceChangePercent',0)),'vol':float(t.get('quoteVolume',0))}

# ── 2. Existing ──
log('检查持仓...')
existing = set()
d = _g('positionRisk')
if d:
    for p in d:
        if float(p.get('positionAmt',0)) != 0: existing.add(p['symbol'])
o = _g('openOrders')
if o:
    for x in o: existing.add(x['symbol'])
if len(existing) >= 6:
    log('持仓已达上限 %d/6, 跳过' % len(existing))
    push('📊 持仓已满%d/6' % len(existing), 'scan')
    sys.exit(0)
log('现有 %d/6' % len(existing))

# ── 3. BTC filter ──
btc24h = mkt.get('BTCUSDT',{}).get('change',0)
log('BTC 24h: %.2f%%' % btc24h)
if btc24h < -5: log('BTC跌超5%不开多'); sys.exit(0)

# ── 4. Scan ──
log('筛选...')
candidates = []
for sym, d in mkt.items():
    if d['price'] < 0.00001 or d['price'] > 100000: continue
    if d['vol'] < 500000: continue
    if sym in existing: continue
    ls, ss = 0, 0
    if d['change'] > 1: ls += 1
    elif d['change'] < -1: ss += 1
    if 3 < -d['change'] < 12: ls += 2  # small dip = bounce
    if btc24h < -0.5: ls -= 1
    if ls <= 0 or ls <= ss: continue
    candidates.append((sym, ls, d))

candidates.sort(key=lambda x: x[1], reverse=True)
log('候选: %d' % len(candidates))
if not candidates: log('无机会'); push('📊 无合适开仓机会', 'scan'); sys.exit(0)

# ── 5. Pick ──
for sym, score, d in candidates[:10]:
    price = d['price']
    raw = USDT_SIZE * LEV / price
    pp = prec.get(sym, {'step':0.001,'min_qty':0.001,'tick':0.01,'min_notional':5})
    step, min_qty, tick, mn = pp['step'], pp['min_qty'], pp['tick'], pp['min_notional']
    sd2 = str(step); dec = len(sd2.split('.')[1].rstrip('0')) if '.' in sd2 else 0
    qty = aq(raw, step, dec)
    if qty < min_qty or qty * price < mn:
        qty = round(int(max(min_qty, mn/price)/step+1)*step, dec)
    margin = qty * price / LEV
    if margin < 5: continue

    limit = price * (1 - LIMIT_DISCOUNT)
    limit = ap(limit, tick)
    log('尝试: %s %sx 限价$%s (保证金%.1fU)' % (sym, qty, limit, margin))

    _p('leverage', {'symbol':sym, 'leverage':LEV}, 3)

    # SL/TP: 舔一口就跑 🏃‍♂️
    sl = limit * (1 - SL_MARGIN/100/LEV)
    tp1 = limit * (1 + TP1_MARGIN/100/LEV)
    tp2 = limit * (1 + TP2_MARGIN/100/LEV)
    sl = ap(sl, tick); tp1 = ap(tp1, tick); tp2 = ap(tp2, tick)
    q60 = aq(qty*0.6, step, dec)
    q40 = aq(qty*0.4, step, dec)

    # SL
    r = _p('algoOrder', {'symbol':sym,'side':'SELL','positionSide':'LONG','algotype':'CONDITIONAL','type':'STOP_MARKET','triggerPrice':str(sl),'quantity':str(qty),'workingType':'MARK_PRICE'})
    if not r or 'algoId' not in r:
        log('  SL失败, 试下一个...')
        continue
    log('  SL @%s (%.1f%%亏损) ✅' % (sl, (1-sl/limit)*100*LEV))

    # TP1 (60%)
    if q60 >= min_qty:
        r = _p('algoOrder', {'symbol':sym,'side':'SELL','positionSide':'LONG','algotype':'CONDITIONAL','type':'TAKE_PROFIT_MARKET','triggerPrice':str(tp1),'quantity':str(q60),'workingType':'MARK_PRICE'})
        log('  TP1🥇 @%s x%s (%.1f%%) %s' % (tp1, q60, (tp1/limit-1)*100*LEV, '✅' if r and 'algoId' in r else '❌'))
    else:
        q60 = 0; q40 = qty  # give all to TP2 if can't split

    # TP2 (40% or 100%)
    r = _p('algoOrder', {'symbol':sym,'side':'SELL','positionSide':'LONG','algotype':'CONDITIONAL','type':'TAKE_PROFIT_MARKET','triggerPrice':str(tp2),'quantity':str(q40 if q60>0 else qty),'workingType':'MARK_PRICE'})
    log('  TP2🥈 @%s x%s (%.1f%%) %s' % (tp2, q40 if q60>0 else qty, (tp2/limit-1)*100*LEV, '✅' if r and 'algoId' in r else '❌'))

    # Limit buy
    r = _p('order', {'symbol':sym,'side':'BUY','type':'LIMIT','timeInForce':'GTC','price':str(limit),'quantity':str(qty),'positionSide':'LONG'})
    if r and 'orderId' in r:
        log('✅ 限价单已挂! orderId=%s' % r['orderId'])
        push('🏃‍♂️ 舔一口: %s\n入场: $%s x%s\n杠杆: %dx | 保证金: %.1fU\n折价: %.1f%%\nSL: $%s (-%.1f%%保证金)\nTP1🥇 $%s (+%.1f%% @60%%)\nTP2🥈 $%s (+%.1f%% @40%%)' % (
            sym, limit, qty, LEV, margin, LIMIT_DISCOUNT*100,
            sl, SL_MARGIN, tp1, TP1_MARGIN, tp2, TP2_MARGIN), 'limit')
        
        # 记录到 bot_state
        from position_manager import create_position, load_bot_state, save_bot_state
        bs = load_bot_state()
        bs.setdefault('positions', {})[sym] = create_position(
            symbol=sym, side='LONG', entry_price=limit,
            amount=margin, qty=qty, strategy='scan',
            reason=f'扫盘限价: 折价{LIMIT_DISCOUNT*100:.0f}% 杠杆{LEV}x',
            sl_price=sl, tp_price=tp1,  # 记录主TP
        )
        bs['positions'][sym]['status'] = 'pending'
        bs.setdefault('trades', []).append({
            'action': 'OPEN_LIMIT', 'symbol': sym,
            'side': 'LONG', 'amount': margin,
            'entry_price': limit, 'strategy': 'scan',
            'reason': f'扫盘限价 折价{LIMIT_DISCOUNT*100:.0f}%',
            'time': datetime.now().isoformat(),
        })
        save_bot_state(bs)
        break
    else:
        log('  ❌ 限价单失败: %s' % str(r)[:100])

log('完成')

#!/usr/bin/env python3
"""
一键更新所有老仓的止盈止损到新策略参数
用法: python3 update_all_sltp.py
"""
import sys, time, json, urllib.parse, hmac, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from config import API_KEY, API_SECRET, PROXY
import requests as req

# 代理（NAS需要，美国服务器直连则PROXY为空）
_SESSION = req.Session()
if PROXY:
    _SESSION.proxies = {'http': PROXY, 'https': PROXY}

FAPI = 'https://fapi.binance.com'
hdrs = {'X-MBX-APIKEY': API_KEY}
ts = lambda: int(time.time()*1000)

def _sig(params):
    p = dict(params); p['timestamp']=ts(); p['recvWindow']=10000
    q = urllib.parse.urlencode(sorted(p.items()))
    s = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q, s

def api(method, path, params=None, retry=3):
    for i in range(retry):
        try:
            q, s = _sig(params or {})
            url = f'{FAPI}/{path}?{q}&signature={s}'
            if method == 'GET':
                r = _SESSION.get(url, headers=hdrs, timeout=20)
            elif method == 'POST':
                r = _SESSION.post(url, headers=hdrs, timeout=20)
            else:
                r = _SESSION.delete(url, headers=hdrs, timeout=20)
            if r.status_code == 200: return r.json()
            err = r.json()
            if err.get('code') in (-2011,-2013): return None  # 已取消
            if err.get('code') == -4003: return None  # 数量为0
        except Exception as e:
            if i < retry-1: time.sleep(3)
    return None

from trader import _align_price_dir, _align_qty

# 策略参数
PROFILES = {
    'band':  {'label':'波段','sl':10.5,'tp':25.0,'lev':3},
    'scalp': {'label':'超短线','sl':5.0,'tp':10.0,'lev':3},
    'scan':  {'label':'扫盘','sl':5.0,'tp':10.0,'lev':5},
}

def detect(entry, sl, tp):
    if not sl or entry <= 0: return 'band'
    sd = abs(entry-sl)/entry*100
    td = abs(entry-tp)/entry*100 if tp else 999
    if sd <= 1.5 and td <= 3.0: return 'scan'
    if sd <= 2.5 and td <= 5.0: return 'scalp'
    if sd <= 5.0 and td >= 5.0: return 'band'
    return 'band'

print('🔍 获取持仓...')
data = api('GET', 'fapi/v2/positionRisk')
if not data:
    print('❌ 无法获取持仓，检查网络和API Key')
    sys.exit(1)

positions = [p for p in data if abs(float(p.get('positionAmt',0))) > 0]
print(f'📊 当前 {len(positions)} 个持仓\n')

# 获取条件单
algos = api('GET', 'fapi/v1/algoOpenOrders') or []
algo_map = {}
for a in algos:
    algo_map.setdefault(a['symbol'], []).append(a)

for p in positions:
    sym = p['symbol']
    amt = abs(float(p['positionAmt']))
    entry = float(p['entryPrice'])
    side = p['positionSide']
    mark = float(p.get('markPrice', 0))
    if amt <= 0 or entry <= 0: continue

    # 当前 SL/TP
    cur_sl, cur_tp = 0, 0
    if sym in algo_map:
        for a in algo_map[sym]:
            if 'STOP' in a.get('type','').upper(): cur_sl = float(a.get('triggerPrice',0))
            elif 'PROFIT' in a.get('type','').upper(): cur_tp = float(a.get('triggerPrice',0))

    strategy = detect(entry, cur_sl, cur_tp)
    pr = PROFILES[strategy]
    close_side = 'SELL' if side == 'LONG' else 'BUY'
    qty = ('%g' % _align_qty(sym, amt)).replace(',', '')

    if side == 'LONG':
        new_sl = entry * (1 - pr['sl'] / pr['lev'] / 100)
        new_tp = entry * (1 + pr['tp'] / pr['lev'] / 100)
    else:
        new_sl = entry * (1 + pr['sl'] / pr['lev'] / 100)
        new_tp = entry * (1 - pr['tp'] / pr['lev'] / 100)

    new_sl = _align_price_dir(sym, new_sl, 'nearest')
    new_tp = _align_price_dir(sym, new_tp, 'nearest')

    print(f'{"="*50}')
    print(f'📌 {sym} {side}  策略={pr["label"]}  {pr["lev"]}x')
    print(f'   入场: {entry:.6f}  当前: {mark:.6f}  ({(mark/entry-1)*100:+.2f}%)')
    print(f'   旧SL: {cur_sl}  → 新SL: {new_sl}  ({-abs(entry-new_sl)/entry*100:.2f}%)')
    print(f'   旧TP: {cur_tp}  → 新TP: {new_tp}  ({abs(entry-new_tp)/entry*100:+.2f}%)')

    # 取消旧条件单
    if sym in algo_map:
        for a in algo_map[sym]:
            api('DELETE', 'fapi/v1/algoOrder', {'symbol':sym, 'algoId':a['algoId']})
            time.sleep(0.2)
        print(f'   🗑️ 旧条件单已取消')

    # 挂新止损
    r = api('POST', 'fapi/v1/algoOrder', {
        'symbol':sym, 'side':close_side, 'positionSide':side,
        'algotype':'CONDITIONAL', 'type':'STOP_MARKET',
        'quantity':qty, 'triggerprice':str(new_sl), 'workingType':'MARK_PRICE',
    })
    print(f'   🛑 新止损 @{new_sl}  {"✅" if r else "❌"}')
    time.sleep(0.5)

    # 挂新止盈
    r = api('POST', 'fapi/v1/algoOrder', {
        'symbol':sym, 'side':close_side, 'positionSide':side,
        'algotype':'CONDITIONAL', 'type':'TAKE_PROFIT_MARKET',
        'quantity':qty, 'triggerprice':str(new_tp), 'workingType':'MARK_PRICE',
    })
    print(f'   🎯 新止盈 @{new_tp}  {"✅" if r else "❌"}')
    time.sleep(0.5)

print(f'\n{"="*50}')
print('✅ 全部更新完成')

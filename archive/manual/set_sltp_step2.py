"""Step 2: 查持仓设止盈止损"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _call(method, path, params=None):
    for i in range(10):
        try:
            p = dict(params or {})
            p['timestamp'] = int(time.time() * 1000)
            p['recvWindow'] = 10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f'{FAPI}/{path}?{q}&signature={sig}'
            if method == 'GET':
                r = req.get(url, headers=hdrs, timeout=20, proxies=prox)
            else:
                r = req.post(url, headers=hdrs, timeout=20, proxies=prox)
            if r.status_code not in (200,):
                return r.json() if r.text else {"_error": f'{r.status_code}'}
            return r.json()
        except Exception as e:
            if i < 9:
                time.sleep(5)
    return None

# Check account positions for our symbols
targets = ['KORUUSDT', 'SYNUSDT', 'SOXLUSDT']
configs = {
    'KORUUSDT': {'sl_pct': 5.0, 'tp_pct': 12.0},
    'SYNUSDT': {'sl_pct': 6.0, 'tp_pct': 15.0},
    'SOXLUSDT': {'sl_pct': 5.0, 'tp_pct': 12.0},
}

print('📊 查询持仓...', flush=True)
acct = _call('GET', 'account')
if not acct:
    print('❌ 无法获取账户信息', flush=True)
    sys.exit(1)

positions = acct.get('positions', [])
print(f'账户余额: {acct.get("availableBalance", "?")} USDT', flush=True)

for pos in positions:
    sym = pos['symbol']
    if sym not in targets:
        continue
    amt = float(pos.get('positionAmt', 0))
    if amt == 0:
        print(f'{sym}: 无持仓', flush=True)
        continue
    entry = float(pos.get('entryPrice', 0))
    side = pos.get('positionSide', '?')
    upnl = float(pos.get('unRealizedProfit', 0))
    liq = pos.get('liquidationPrice', 'N/A')
    print(f'\n{sym}: {side} {amt}张 entry={entry:.4f} upnl={upnl:.4f} liq={liq}', flush=True)

    cfg = configs.get(sym, {'sl_pct': 5.0, 'tp_pct': 10.0})

    ex = _call('GET', 'exchangeInfo')
    si = next((s for s in ex.get('symbols', []) if s['symbol'] == sym), None)
    tick = 0.01
    if si:
        flt = {f['filterType']: f for f in si['filters']}
        tick = float(flt['PRICE_FILTER']['tickSize'])

    sl_p = entry * (1 - cfg['sl_pct'] / 100)
    tp_p = entry * (1 + cfg['tp_pct'] / 100)
    sl_p = round(int(sl_p / tick + 0.5) * tick, 8)
    tp_p = round(int(tp_p / tick + 0.5) * tick, 8)

    print(f'  止损@{sl_p} 止盈@{tp_p}', flush=True)

    sl = _call('POST', 'algoOrder', {
        'symbol': sym, 'side': 'SELL', 'positionSide': side,
        'algotype': 'CONDITIONAL', 'type': 'STOP_MARKET',
        'triggerPrice': str(sl_p), 'quantity': str(amt),
        'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
    })
    sl_ok = sl and 'algoId' in sl
    print(f'  止损: {"✅" if sl_ok else "❌"} {sl}', flush=True)

    tp = _call('POST', 'algoOrder', {
        'symbol': sym, 'side': 'SELL', 'positionSide': side,
        'algotype': 'CONDITIONAL', 'type': 'TAKE_PROFIT_MARKET',
        'triggerPrice': str(tp_p), 'quantity': str(amt),
        'workingType': 'MARK_PRICE', 'timeInForce': 'GTE_GTC',
    })
    tp_ok = tp and 'algoId' in tp
    print(f'  止盈: {"✅" if tp_ok else "❌"} {tp}', flush=True)

print('\n✅ 止盈止损设置完成', flush=True)

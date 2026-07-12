"""开三单小仓位短线: KORUUSDT + SYNUSDT + SOXLUSDT"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req

prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _api(method, path, params=None):
    for i in range(12):
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
            if r.status_code != 200:
                raise Exception(f'{r.status_code}: {r.text[:200]}')
            return r.json()
        except Exception as e:
            if i < 11:
                print(f'  {i+1}: {type(e).__name__}', flush=True)
                time.sleep(5)
            else:
                return None
    return None

def get_precision(sym):
    """Get step/tick/minQty for a symbol"""
    ex = _api('GET', 'exchangeInfo')
    for s in ex.get('symbols', []):
        if s['symbol'] == sym:
            flt = {f['filterType']: f for f in s['filters']}
            step = float(flt['LOT_SIZE']['stepSize'])
            min_qty = float(flt['LOT_SIZE']['minQty'])
            tick = float(flt['PRICE_FILTER']['tickSize'])
            mn = float(flt.get('MIN_NOTIONAL', {}).get('notional', 5))
            return step, min_qty, tick, mn
    return 0.001, 0.001, 0.01, 5

def calc_qty(usdt_val, price, step, min_qty, min_notional):
    """Calculate quantity for a given USDT position value"""
    raw = usdt_val / price
    ss = str(step)
    dec = len(ss.split('.')[1].rstrip('0')) if '.' in ss else 0
    qty = int(raw / step) * step
    qty = round(qty, dec)
    if qty < min_qty or qty * price < min_notional:
        qty = max(min_qty, min_notional / price)
        qty = round(int(qty / step + 1) * step, dec)
    return qty, dec

# Config for each coin
orders_config = [
    {
        "symbol": "KORUUSDT",
        "usdt": 10,        # 10U
        "leverage": 3,
        "sl_pct": 5.0,     # 5% stop
        "tp_pct": 12.0,    # 12% take profit
    },
    {
        "symbol": "SYNUSDT",
        "usdt": 10,
        "leverage": 3,
        "sl_pct": 6.0,
        "tp_pct": 15.0,
    },
    {
        "symbol": "SOXLUSDT",
        "usdt": 10,
        "leverage": 3,
        "sl_pct": 5.0,
        "tp_pct": 12.0,
    },
]

results = []

for cfg in orders_config:
    sym = cfg["symbol"]
    print(f'\n{"="*40}', flush=True)
    print(f'🚀 {sym} 开多中...', flush=True)
    
    # Price
    d = _api('GET', 'ticker/price', {'symbol': sym})
    if not d or 'price' not in d:
        print(f'  ❌ 价格获取失败', flush=True)
        continue
    price = float(d['price'])
    print(f'  价格: ${price:.4f}', flush=True)
    
    # Precision
    step, min_qty, tick, min_not = get_precision(sym)
    print(f'  精度: step={step} min_qty={min_qty} tick={tick}', flush=True)
    
    # Leverage
    _api('POST', 'leverage', {'symbol': sym, 'leverage': cfg['leverage']})
    
    # Margin mode
    try:
        _api('POST', 'marginType', {'symbol': sym, 'marginType': 'CROSSED'})
    except:
        pass
    
    # Quantity
    pos_val = cfg['usdt'] * cfg['leverage']
    qty, dec = calc_qty(pos_val, price, step, min_qty, min_not)
    actual_usdt = qty * price / cfg['leverage']
    print(f'  数量: {qty} (价值{qty*price:.2f}U, 保证金{actual_usdt:.2f}U)', flush=True)
    
    # Market order
    order = _api('POST', 'order', {
        'symbol': sym, 'side': 'BUY',
        'type': 'MARKET', 'quantity': str(qty),
        'positionSide': 'LONG',
    })
    if not order:
        print(f'  ❌ 下单失败', flush=True)
        continue
    
    entry = float(order.get('avgPrice', price))
    filled = float(order.get('executedQty', qty))
    print(f'  ✅ 成交! entry={entry:.4f} filled={filled}', flush=True)
    
    # SL/TP
    sl_price = entry * (1 - cfg['sl_pct'] / 100)
    tp_price = entry * (1 + cfg['tp_pct'] / 100)
    sl_price = round(int(sl_price / tick + 0.5) * tick, 8)
    tp_price = round(int(tp_price / tick + 0.5) * tick, 8)
    print(f'  🛡️ 止损@{sl_price:.4f} (-{cfg["sl_pct"]}%) 止盈@{tp_price:.4f} (+{cfg["tp_pct"]}%)', flush=True)
    
    # Place SL
    sl = _api('POST', 'algoOrder', {
        'symbol': sym, 'side': 'SELL',
        'positionSide': 'LONG', 'algotype': 'CONDITIONAL',
        'type': 'STOP_MARKET', 'triggerPrice': str(sl_price),
        'quantity': str(filled), 'workingType': 'MARK_PRICE',
        'timeInForce': 'GTE_GTC',
    })
    sl_ok = sl and 'algoId' in sl
    print(f'  {"✅" if sl_ok else "❌"} 止损: {sl.get("algoId","失败") if sl else "失败"}', flush=True)
    
    # Place TP
    tp = _api('POST', 'algoOrder', {
        'symbol': sym, 'side': 'SELL',
        'positionSide': 'LONG', 'algotype': 'CONDITIONAL',
        'type': 'TAKE_PROFIT_MARKET', 'triggerPrice': str(tp_price),
        'quantity': str(filled), 'workingType': 'MARK_PRICE',
        'timeInForce': 'GTE_GTC',
    })
    tp_ok = tp and 'algoId' in tp
    print(f'  {"✅" if tp_ok else "❌"} 止盈: {tp.get("algoId","失败") if tp else "失败"}', flush=True)
    
    results.append({
        "symbol": sym,
        "entry": entry,
        "filled": filled,
        "margin": actual_usdt,
        "sl": sl_price,
        "tp": tp_price,
        "sl_ok": sl_ok,
        "tp_ok": tp_ok,
    })

# Summary
print(f'\n{"="*40}', flush=True)
print('📋 开仓汇总', flush=True)
total_margin = 0
for r in results:
    status = '✅' if r['sl_ok'] and r['tp_ok'] else '⚠️'
    total_margin += r['margin']
    print(f'  {status} {r["symbol"]}: ${r["entry"]:.4f} x{r["filled"]} = {r["margin"]:.2f}U', flush=True)
    print(f'     止损${r["sl"]:.4f} / 止盈${r["tp"]:.4f}', flush=True)

print(f'\n总保证金: {total_margin:.2f}U', flush=True)

# Notifications
from notifications import push
for r in results:
    push(
        f'🚀 开多: {r["symbol"]} LONG\n'
        f'入场: {r["entry"]:.4f}\n'
        f'数量: {r["filled"]}\n'
        f'保证金: {r["margin"]:.2f}U\n'
        f'止损: {r["sl"]:.4f}\n'
        f'止盈: {r["tp"]:.4f}',
        'open'
    )

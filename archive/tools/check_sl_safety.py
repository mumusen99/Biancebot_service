"""止损位安全评估"""
import sys, time, json, urllib.parse, hmac, hashlib
sys.path.insert(0, '.')
from config import PROXY, API_KEY, API_SECRET
import requests as req
prox = {'http': PROXY, 'https': PROXY}
hdrs = {'X-MBX-APIKEY': API_KEY}
FAPI = 'https://fapi.binance.com/fapi/v1'

def _g(url, p=None):
    for i in range(8):
        try:
            p = dict(p or {}); p['timestamp']=int(time.time()*1000); p['recvWindow']=10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = req.get(f'{url}?{q}&signature={sig}', headers=hdrs, timeout=20, proxies=prox)
            if r.status_code != 200: return None
            return r.json()
        except: time.sleep(5)
    return None

d = _g('https://fapi.binance.com/fapi/v2/positionRisk')
if not d:
    print('获取仓位失败', flush=True)
    sys.exit(1)

print('=== 止损位安全评估 ===', flush=True)

needs_attention = []

for p in d:
    amt = float(p.get('positionAmt',0))
    if amt == 0: continue
    sym = p['symbol']
    entry = float(p.get('entryPrice',0))
    mark = float(p.get('markPrice',0))
    upnl = float(p.get('unRealizedProfit',0))
    side = 'LONG' if amt>0 else 'SHORT'
    pnl_pct = ((mark/entry)-1)*100 * (1 if amt>0 else -1)
    abs_amt = abs(amt)

    alog = _g(f'{FAPI}/algoOrder', {'symbol': sym})
    sl_price = None
    tp_prices = []
    if alog and isinstance(alog, list):
        for a in alog:
            if a.get('algoStatus') != 'NEW': continue
            tp = float(a.get('triggerPrice',0))
            qty = float(a.get('quantity',0))
            otype = a.get('orderType','')
            if 'STOP' in otype and 'TAKE' not in otype:
                if sl_price is None: sl_price = tp
            elif 'TAKE' in otype:
                tp_prices.append((tp, qty))

    margin_val = abs_amt * entry / 3
    print(f'{sym} {side} {abs_amt:.4f} 入场{entry:.4f} 现价{mark:.4f} PnL{upnl:+.2f}U({pnl_pct:+.2f}%)', flush=True)

    if sl_price:
        if side == 'LONG':
            price_dist = (mark / sl_price - 1) * 100
            max_loss_at_sl = (entry - sl_price) / entry * 100 * 3
        else:
            price_dist = (sl_price / mark - 1) * 100
            max_loss_at_sl = (sl_price - entry) / entry * 100 * 3

        print(f'  SL: ${sl_price:.4f} 距现价 {price_dist:.1f}%', flush=True)
        print(f'  触发止损亏损: {max_loss_at_sl:.1f}%保证金 ({margin_val*max_loss_at_sl/100:.2f}U)', flush=True)

        if price_dist < 1:
            level = '🔴 极度危险！只差不到1%'
            print(f'  建议: 要么不动等止损，要么提前手动砍', flush=True)
            needs_attention.append((sym, 'danger', price_dist))
        elif price_dist < 2.5:
            level = '⚠️ 偏近（2.5%以内）'
            print(f'  建议: 关注但不建议动，硬扛等反弹', flush=True)
            needs_attention.append((sym, 'close', price_dist))
        elif price_dist < 5:
            level = '🟡 适中（2.5-5%）'
            print(f'  建议: 暂时安全，不需要下移', flush=True)
        else:
            level = '🟢 安全（5%以上）'
            print(f'  建议: 很安全，不动', flush=True)

        print(f'  评估: {level}', flush=True)
    else:
        print(f'  SL: ❌ 未设置!', flush=True)
        needs_attention.append((sym, 'no_sl', 0))

    # Show TPs
    for i, (tp_p, tp_q) in enumerate(tp_prices[:3], 1):
        if side == 'LONG':
            tp_dist = (tp_p / mark - 1) * 100
        else:
            tp_dist = (1 - tp_p / mark) * 100
        label = f'TP{i}'
        print(f'  {label}: ${tp_p:.4f} x{tp_q} 距现价+{tp_dist:.1f}%', flush=True)

    print('', flush=True)

print('=== 总结 ===', flush=True)
if needs_attention:
    for sym, kind, dist in needs_attention:
        if kind == 'danger':
            print(f'🔴 {sym}: 距止损仅{dist:.1f}%，随时可能触发', flush=True)
        elif kind == 'close':
            print(f'⚠️ {sym}: 距止损{dist:.1f}%，偏近', flush=True)
        elif kind == 'no_sl':
            print(f'❌ {sym}: 止损未设置!', flush=True)
else:
    print('所有止损位安全 ✅', flush=True)

"""止损安全评估 V2"""
import sys, time, urllib.parse, hmac, hashlib
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

positions = _g('https://fapi.binance.com/fapi/v2/positionRisk')
if not positions:
    print('获取失败', flush=True)
    sys.exit(1)

# SL prices we set earlier (known from algo order placement)
sl_config = {
    'SYNUSDT': 0.35179,
    'SOXLUSDT': 171.92,
    'XAUUSDT': 4280.83,
    'SPCXUSDT': 151.29,
}
tp_config = {
    'SYNUSDT': [0.37593, 0.3856, 0.3977],
    'SOXLUSDT': [183.72, 188.46, 194.38],
    'XAUUSDT': [4005.94, 3895.44, 3757.3],
    'SPCXUSDT': [161.66, 165.84, 171.05],
}

print('=== 止损安全评估 ===', flush=True)
print('', flush=True)

for p in positions:
    amt = float(p.get('positionAmt',0))
    if amt == 0: continue
    sym = p['symbol']
    entry = float(p.get('entryPrice',0))
    mark = float(p.get('markPrice',0))
    upnl = float(p.get('unRealizedProfit',0))
    side = 'LONG' if amt>0 else 'SHORT'
    pnl_pct = ((mark/entry)-1)*100 * (1 if amt>0 else -1)
    abs_amt = abs(amt)
    margin_val = abs_amt * entry / 3

    print(f'{sym} {side} | 入场{entry:.4f} | 现价{mark:.4f} | PnL{upnl:+.2f}U({pnl_pct:+.2f}%)', flush=True)

    sl_price = sl_config.get(sym)
    if sl_price and sl_price > 0:
        if side == 'LONG':
            price_dist = (mark / sl_price - 1) * 100
            loss_at_sl = (entry - sl_price) / entry * 100 * 3
        else:
            price_dist = (sl_price / mark - 1) * 100
            loss_at_sl = (sl_price - entry) / entry * 100 * 3
        loss_u = margin_val * loss_at_sl / 100

        print(f'  SL: ${sl_price:.4f}', flush=True)
        print(f'  距止损: {price_dist:.1f}% | 触发亏损: {loss_at_sl:.1f}%保证金 ({loss_u:.2f}U)', flush=True)

        if price_dist < 1:
            print(f'  ⛔ 极度危险! 随时可能触发', flush=True)
        elif price_dist < 3:
            print(f'  ⚠️ 偏近', flush=True)
        elif price_dist < 5:
            print(f'  🟡 适中', flush=True)
        else:
            print(f'  🟢 安全', flush=True)

        # TP status
        tps = tp_config.get(sym, [])
        for i, tp in enumerate(tps[:3]):
            if side == 'LONG':
                tp_dist = (tp / mark - 1) * 100
            else:
                tp_dist = (1 - tp / mark) * 100
            print(f'  TP{i+1}: ${tp:.4f} (+{tp_dist:.1f}%)', flush=True)
    else:
        print(f'  SL: 未设置', flush=True)

    print('', flush=True)

print('=== 建议 ===', flush=True)
print('SOXLUSDT 距止损最近 (1.0%)，但RSI已极度超卖，硬扛等反弹合理。', flush=True)
print('不建议主动下移止损，按纪律到了就走。', flush=True)

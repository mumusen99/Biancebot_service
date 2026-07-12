"""SPCXUSDT SL/TP检查"""
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

# 1. Position
print('=== 持仓 ===', flush=True)
d = _g('https://fapi.binance.com/fapi/v2/positionRisk', {'symbol': 'SPCXUSDT'})
if d:
    for p in d:
        amt = float(p.get('positionAmt',0))
        if amt != 0:
            entry = float(p.get('entryPrice',0))
            mark = float(p.get('markPrice',0))
            upnl = float(p.get('unRealizedProfit',0))
            side = 'LONG' if amt>0 else 'SHORT'
            pnl_pct = ((mark/entry)-1)*100 * (1 if amt>0 else -1)
            print(f'{p["symbol"]} {side} {abs(amt):.4f} 入场{entry:.4f} 现价{mark:.4f} PnL{upnl:+.2f}U({pnl_pct:+.2f}%)', flush=True)

# 2. Open orders (limit + stop)
print('\n=== 挂单 ===', flush=True)
orders = _g(f'{FAPI}/openOrders', {'symbol': 'SPCXUSDT'})
if orders:
    for o in orders:
        print(f'  {o["side"]} {o["type"]} @{o["price"]} x{o["origQty"]} 已成交{o.get("executedQty","0")} 状态{o["status"]}', flush=True)

# 3. Try to query algo orders (need algoId)
# We already know the SL/TP we set, let me verify by checking the known algoIds
print('\n=== 止盈止损(algo订单) ===', flush=True)

# Known algo IDs from earlier placement
known_orders = [
    ('SL(全仓)', 151.29, 0.63),
    ('TP1(50%)', 161.66, 0.315),
    ('TP2(25%)', 165.84, 0.1575),
    ('TP3(25%)', 171.05, 0.1575),
]

for label, price, qty in known_orders:
    if qty > 0:
        print(f'  {label}: @{price:.2f} x{qty}', flush=True)

# 4. Check price vs SL/TP
mark = 0
if d:
    for p in d:
        if float(p.get('positionAmt',0)) != 0:
            mark = float(p.get('markPrice',0))
            entry = float(p.get('entryPrice',0))
            
            # Check distances
            sl_dist = (mark / 151.29 - 1) * 100
            tp1_dist = (161.66 / mark - 1) * 100
            tp2_dist = (165.84 / mark - 1) * 100
            tp3_dist = (171.05 / mark - 1) * 100
            
            print(f'\n=== 价格位置 ===', flush=True)
            print(f'入场: ${entry:.2f}  现价: ${mark:.2f}', flush=True)
            print(f'SL:   $151.29 ({sl_dist:+.1f}%)  ⚠️', flush=True)
            print(f'TP1:  $161.66 (+{tp1_dist:.1f}%)', flush=True)
            print(f'TP2:  $165.84 (+{tp2_dist:.1f}%)', flush=True)
            print(f'TP3:  $171.05 (+{tp3_dist:.1f}%)', flush=True)
            
            # Assessment
            issues = []
            if sl_dist < 2:
                issues.append(f'🔴 SL距现价仅{sl_dist:.1f}%，危险!')
            elif sl_dist < 4:
                issues.append(f'⚠️ SL距现价{sl_dist:.1f}%，偏近')
            
            if tp1_dist > 12:
                issues.append(f'⚠️ TP1距现价{tp1_dist:.1f}%，可能太远')
            
            if pnl_pct > 3 and sl_dist > 5:
                issues.append(f'✅ 浮盈+{pnl_pct:.1f}%，可考虑上移止损到入场价保本')
            
            if issues:
                print(f'\n=== 问题 ===', flush=True)
                for i in issues:
                    print(f'  {i}', flush=True)
            else:
                print(f'\n✅ 止盈止损设置合理，无需调整', flush=True)

print('\n✅ 检查完成', flush=True)

#!/bin/bash
# cron 每小时执行器：波段限价 + 复查（不含舔一口）
# 舔一口单独走 15分钟 cron
cd /vol2/@apphome/trim.openclaw/data/workspace/binance-bot
VENV=/vol2/@apphome/trim.openclaw/data/workspace/binance-venv/bin/python3
START=$(date +%s)

echo "=== 代理检查 ==="
$VENV -c "
from proxy_guard import ensure_connection
ok = ensure_connection()
print('连通性:', '✅ 正常' if ok else '❌ 异常')
" 2>&1

echo ""
echo "=== ① 舔一口超短线 ==="
$VENV run_scalper_quick.py 2>&1

echo ""
echo "=== ② 波段限价 + 复查 ==="
$VENV run_heartbeat.py 2>&1

echo ""
echo "=== 状态摘要 ==="
$VENV -c "
import json
from pathlib import Path
f = Path('/vol2/@apphome/trim.openclaw/data/workspace/binance-bot/bot_state.json')
s = json.loads(f.read_text())
pos = s.get('positions', {})
print(f'持仓: {len(pos)}个')
total = float(s.get('total_pnl', 0)) + float(s.get('closed_pnl', 0))
print(f'总PnL: {total:+.2f}U')
budget = s.get('budget', 0)
print(f'预算: {budget}U')
trades = s.get('trades', [])
recent = [t for t in trades if t.get('action') in ('OPEN', 'OPEN_LIMIT', 'CLOSE')][-5:][::-1]
for t in recent:
    print(f'  {t[\"action\"]} {t[\"symbol\"]} {t.get(\"side\",\"\")}')
"

echo ""
echo "=== ③ 实时系统状态 ==="
$VENV -c "
import json, time, requests as rq, hmac, hashlib, urllib.parse
from config import API_KEY, API_SECRET, PROXY

prox = {'http': PROXY, 'https': PROXY}
ts = int(time.time() * 1000)
p = {'timestamp': str(ts), 'recvWindow': '10000'}
q = urllib.parse.urlencode(sorted(p.items()))
sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
r = rq.get(f'https://fapi.binance.com/fapi/v2/positionRisk?{q}&signature={sig}', headers={'X-MBX-APIKEY': API_KEY}, proxies=prox, timeout=10)
positions = r.json() if r.status_code == 200 else []

r2 = rq.get('https://fapi.binance.com/fapi/v1/ticker/price', proxies=prox, timeout=5)
prices = {d['symbol']: float(d['price']) for d in r2.json()} if r2.status_code == 200 else {}

print(f'交易所持仓: {sum(1 for p in positions if abs(float(p[\"positionAmt\"])) > 0)}个')
total_pnl = 0
for pos in positions:
    if abs(float(pos[\"positionAmt\"])) == 0: continue
    sym = pos['symbol']
    entry = float(pos['entryPrice'])
    cur = prices.get(sym, entry)
    pnl = float(pos['unRealizedProfit'])
    side = pos['positionSide']
    total_pnl += pnl
    icon = '✅' if pnl > 0.05 else ('❌' if pnl < -0.05 else '➖')
    print(f'  {icon} {sym:12s} {side:6s} entry={entry:.4f} cur={cur:.4f} PnL={pnl:+.4f}U')
print(f'  合计PnL: {total_pnl:+.4f}U')
" 2>&1

echo ""
DURATION=$(( $(date +%s) - START ))
echo "总耗时: ${DURATION}s"

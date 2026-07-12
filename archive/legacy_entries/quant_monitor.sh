#!/bin/bash
cd /vol2/@apphome/trim.openclaw/data/workspace/binance-bot
VENV=/vol2/@apphome/trim.openclaw/data/workspace/binance-venv/bin/python3
PID=$(pgrep -f "quant_scalper" | head -1)
if [ -z "$PID" ]; then
    echo "❌ 量化系统未运行"
    exit 1
fi

echo "量化系统 PID: $PID"
echo "运行时间: $(ps -o etime= -p $PID | xargs)"
echo ""

$VENV -c "
import json
s = json.loads(open('bot_state.json').read())
total = float(s.get('total_pnl', 0))
closed = float(s.get('closed_pnl', 0))
print(f'总PnL: {total:+.4f}U')
print(f'累计已实现: {closed:+.4f}U')
pos = s.get('positions', {})
scalp_count = sum(1 for v in pos.values() if v.get(\"strategy\") == 'scalp')
long_count = sum(1 for v in pos.values() if v.get(\"strategy\") != 'scalp')
print(f'scalp持仓: {scalp_count}个 | 长线持仓: {long_count}个')
trades = [t for t in s.get('trades', []) if t.get('strategy') == 'scalp' and t.get('action') in ('OPEN', 'OPEN_LIMIT', 'CLOSE', 'FILLED')][-5:]
for t in reversed(trades):
    print(f'  {t[\"action\"]} {t[\"symbol\"]}')
"
echo ""
tail -3 /vol2/@apphome/trim.openclaw/data/workspace/binance-bot/quant_scalper.log 2>/dev/null

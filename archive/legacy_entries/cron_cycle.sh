#!/bin/bash
# ─────────────────────────────────────────────────────────
# 定时执行脚本 — 每 15 min 由 crontab 调用
# ─────────────────────────────────────────────────────────
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$BASE_DIR/../binance-venv"
PYTHON="$VENV_DIR/bin/python3"
LOG="$BASE_DIR/cron.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 开始周期检查 ===" >> "$LOG"

# 1. 运行分析
$PYTHON "$BASE_DIR/run_check.py" --analyze >> "$LOG" 2>&1

# 2. 执行待处理决策
$PYTHON "$BASE_DIR/run_check.py" --execute >> "$LOG" 2>&1

# 3. AI 自动交易引擎 (独立20U预算)
$PYTHON "$BASE_DIR/auto_trader.py" >> "$LOG" 2>&1

# 4. 检查是否有待发送的每日总结
if [ -f "$BASE_DIR/.summary_pending" ]; then
    echo "📋 每日总结待发送，等待AI上线通知微信" >> "$LOG"
fi

# 5. 追加持仓快照
$PYTHON -c "
import json
try:
    with open('$BASE_DIR/analysis.json') as f:
        r = json.load(f)
    bal = r.get('balance', {})
    pos = r.get('positions', [])
    print(f'余额: \${bal.get(\"free\",0):.2f} | 持仓: {len(pos)}个')
    for p in pos:
        print(f'  {p[\"symbol\"]} {p[\"side\"]} {p[\"size\"]}张 PnL:{p[\"pnl_percent\"]:+.2f}%')
except Exception as e:
    print(f'读取失败: {e}')
" >> "$LOG" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 周期检查完成 ===" >> "$LOG"

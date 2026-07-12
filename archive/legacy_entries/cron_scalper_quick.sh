#!/bin/bash
# cron 15分钟执行器：仅舔一口短线扫描
cd /vol2/@apphome/trim.openclaw/data/workspace/binance-bot
VENV=/vol2/@apphome/trim.openclaw/data/workspace/binance-venv/bin/python3
START=$(date +%s)

echo "=== 舔一口短线扫描 ==="
$VENV run_scalper_quick.py 2>&1

echo ""
DURATION=$(( $(date +%s) - START ))
echo "总耗时: ${DURATION}s"

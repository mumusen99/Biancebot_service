#!/bin/bash
# ─────────────────────────────────────────────────────────
# 每日 22:00 总结报告 — 生成+标记待发送
# ─────────────────────────────────────────────────────────
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$BASE_DIR/../binance-venv"
PYTHON="$VENV_DIR/bin/python3"

$PYTHON -c "
import json, sys
sys.path.insert(0, '$BASE_DIR')
from pathlib import Path

# 读取状态
state_file = Path('$BASE_DIR/bot_state.json')
sentiment_file = Path('$BASE_DIR/sentiment.json')

state = json.loads(state_file.read_text()) if state_file.exists() else {}
sentiment = json.loads(sentiment_file.read_text()) if sentiment_file.exists() else {}

# 构建每日总结
summary = {
    'type': 'daily_summary',
    'date': '$(date +%Y-%m-%d)',
    'generated_at': '$(date +%Y-%m-%dT%H:%M:%S)',
    'bot_pnl': state.get('total_pnl', 0),
    'budget_used': sum(p.get('amount', 0) for p in state.get('positions', {}).values()),
    'budget_remaining': state.get('budget', 20),
    'ai_positions': [],
    'user_positions': [],
    'today_trades': [],
    'kol_sentiment': sentiment.get('overall_sentiment', 'neutral'),
    'stopped': state.get('stopped', False),
}

# 从分析文件读取持仓
import sys as _sys
_sys.path.insert(0, '$BASE_DIR')
from data_fetcher import fetch_positions, fetch_balance
import os
os.environ['BINANCE_API_KEY'] = open('$BASE_DIR/.env').read().split('BINANCE_API_KEY=')[1].split('\n')[0].strip(' \"')
os.environ['BINANCE_API_SECRET'] = open('$BASE_DIR/.env').read().split('BINANCE_API_SECRET=')[1].split('\n')[0].strip(' \"')
os.environ['BINANCE_TESTNET'] = 'false'
import importlib, config as c
importlib.reload(c)

bal = fetch_balance()
summary['account_balance'] = bal.get('free', 0)
summary['account_total'] = bal.get('total', 0)

pos = fetch_positions()
for p in pos:
    entry = {
        'symbol': p['symbol'], 'side': p['side'], 'size': p['size'],
        'entry_price': p['entry_price'], 'pnl': p['pnl'], 'pnl_percent': p['pnl_percent'],
    }
    if p['symbol'] in state.get('positions', {}):
        entry['owner'] = 'AI'
        summary['ai_positions'].append(entry)
    else:
        entry['owner'] = 'user'
        summary['user_positions'].append(entry)

# 今日交易统计
trades = state.get('trades', [])
import datetime
today = datetime.date.today().isoformat()
summary['today_trades'] = [t for t in trades if t.get('time','').startswith(today)]

# 保存并标记待发送
Path('$BASE_DIR/daily_summary.json').write_text(json.dumps(summary, indent=2))
Path('$BASE_DIR/.summary_pending').write_text('1')

# 打印摘要
print(f'=== 每日总结 {summary[\"date\"]} ===')
print(f'AI PnL: {summary[\"bot_pnl\"]:+.2f}U')
print(f'账户余额: {summary[\"account_balance\"]:.2f}U (可用)')
print(f'AI持仓: {len(summary[\"ai_positions\"])}个')
print(f'用户持仓: {len(summary[\"user_positions\"])}个')
print(f'今日交易: {len(summary[\"today_trades\"])}笔')
print(f'KOL情绪: {summary[\"kol_sentiment\"]}')
print('✅ 总结已生成，等待AI发送微信通知')
" >> "$BASE_DIR/cron.log" 2>&1

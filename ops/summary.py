#!/usr/bin/env python3
"""每30分钟生成交易摘要：持仓、盈亏、信号"""
import json, time, hmac, hashlib, urllib.parse, os, sys
from pathlib import Path
from datetime import datetime, timezone

import requests as req

BOT_STATE = Path('/opt/trading-bot/shared/state/bot_state.json')

# 读取 env
env = {}
for line in Path('/opt/trading-bot/current/.env').read_text().strip().split('\n'):
    if '=' in line and not line.strip().startswith('#'):
        k, v = line.strip().split('=', 1)
        env[k] = v.strip().strip('"').strip("'")

API_KEY = env.get('BINANCE_API_KEY', '')
API_SECRET = env.get('BINANCE_API_SECRET', '')

def signed_get(path):
    params = {'timestamp': int(time.time() * 1000), 'recvWindow': 10000}
    q = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f'https://fapi.binance.com{path}?{q}&signature={sig}'
    r = req.get(url, headers={'X-MBX-APIKEY': API_KEY}, timeout=15)
    if r.status_code != 200:
        return None
    return r.json()

def fmt_pnl(v):
    sign = '+' if v >= 0 else ''
    return f'{sign}{v:.2f}'

# ── 交易所数据 ──
account = signed_get('/fapi/v2/account')
balance = '?'
if account:
    balance = account.get('totalWalletBalance', '?')

positions_raw = signed_get('/fapi/v2/positionRisk') or []
live_positions = [p for p in positions_raw if abs(float(p.get('positionAmt', 0))) > 0]

# ── bot_state ──
try:
    bot = json.loads(BOT_STATE.read_text())
except:
    bot = {'positions': {}, 'trades': [], 'total_pnl': 0.0}

scalp_positions = {k: v for k, v in bot.get('positions', {}).items()
                   if v.get('strategy') == 'scalp'}
total_pnl = bot.get('total_pnl', 0)

# ── 生成摘要 ──
now = datetime.now(timezone.utc).strftime('%H:%M UTC')
lines = [f'📊 交易摘要 {now}', '']

# 账户
lines.append(f'💰 余额: {balance} USDT')
lines.append('')

# 活跃持仓
active_count = len(live_positions)
scalp_count = len(scalp_positions)
lines.append(f'📌 活跃持仓: 交易所{active_count}个 / bot追踪{scalp_count}个')

if live_positions:
    for p in live_positions:
        sym = p['symbol']
        amt = float(p.get('positionAmt', 0))
        entry = float(p.get('entryPrice', 0))
        mark = float(p.get('markPrice', 0))
        pnl = float(p.get('unRealizedProfit', 0))
        pnl_pct = (mark - entry) / entry * 100 if entry > 0 else 0
        side = '多' if amt > 0 else '空'
        lines.append(f'  {sym} {side} {abs(amt):.1f}张 @{entry} 现{mark} 盈亏{fmt_pnl(pnl)} ({pnl_pct:+.1f}%)')
else:
    lines.append('  （无持仓）')

# 挂单
lines.append('')
if scalp_positions:
    for sym, info in scalp_positions.items():
        status = info.get('status', '?')
        lines.append(f'  ⏳ {sym} {info.get("side","")} 状态:{status} SL:{info.get("sl_price",0)} TP:{info.get("tp_price",0)}')
else:
    lines.append('📭 无挂单')

# 策略状态
lines.append('')
lines.append(f'📈 累计盈亏: {fmt_pnl(total_pnl)} USDT')

# 最近信号（扫最后5条交易）
recent = [t for t in bot.get('trades', [])][-5:]
if recent:
    lines.append('')
    lines.append('🔄 最近活动:')
    for t in reversed(recent):
        a = t.get('action', '?')
        sym = t.get('symbol', '?')
        pnl = t.get('pnl', 0)
        reason = t.get('reason', '')[:30]
        ts = t.get('time', '')[:16].replace('T', ' ')
        if a in ('OPEN', 'FILLED'):
            lines.append(f'  {ts} ✅ {sym} {t.get("side","")} 开仓 @{t.get("entry_price",0)}')
        elif a == 'CLOSE':
            lines.append(f'  {ts} ❌ {sym} 平仓 {fmt_pnl(pnl)} {reason}')
        else:
            lines.append(f'  {ts} {a} {sym}')

print('\n'.join(lines))

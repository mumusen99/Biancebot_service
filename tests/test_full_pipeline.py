#!/usr/bin/env python3
"""Full pipeline integration test — simulated signal through mock Binance."""
import os, sys, json, requests, time, subprocess, signal

MOCK_HOST = 'http://127.0.0.1:8765'
H = {'X-MBX-APIKEY': 'test'}

# Clean mock state before test
requests.get(f'{MOCK_HOST}/fapi/v1/reset', headers=H, timeout=3)

# Ensure mock is running
r = requests.get(f'{MOCK_HOST}/fapi/v2/account', params={'timestamp': 1, 'signature': 'x'}, headers=H, timeout=3)
if r.status_code != 200:
    print('Start mock first: .venv/bin/python3 tests/mock_binance.py 8765 &')
    sys.exit(1)
print('Mock OK')

ok = 0; fail = 0

def check(cond, msg):
    global ok, fail
    if cond: ok += 1; print(f'  ✅ {msg}')
    else: fail += 1; print(f'  ❌ {msg}')

# ═══════════════════════════════════════════
# 1. Simulate signal → open position
# ═══════════════════════════════════════════
print('\n=== 1. Open position ===')
r = requests.post(f'{MOCK_HOST}/fapi/v1/order', data={
    'symbol': 'BTCUSDT', 'side': 'BUY', 'type': 'MARKET',
    'quantity': '0.1', 'positionSide': 'LONG',
    'timestamp': '1', 'signature': 'x'
}, headers=H, timeout=5)
check(r.status_code == 200, f'order accepted: {r.json().get("status")}')
entry_price = float(r.json().get('avgPrice', r.json().get('price', 0)))
check(entry_price > 0, f'entry price: {entry_price:.2f}')
order_id = r.json().get('orderId')

# ═══════════════════════════════════════════
# 2. Create exchange hard stop-loss
# ═══════════════════════════════════════════
print('\n=== 2. Exchange hard SL ===')
sl_price = round(entry_price * 0.995, 2)  # 0.5% SL
tp_price = round(entry_price * 1.01, 2)   # 1% TP
r = requests.post(f'{MOCK_HOST}/fapi/v1/algoOrder', data={
    'symbol': 'BTCUSDT', 'side': 'SELL', 'positionSide': 'LONG',
    'type': 'STOP_MARKET', 'quantity': '0.1',
    'stopPrice': str(sl_price), 'workingType': 'MARK_PRICE',
    'timestamp': '1', 'signature': 'x'
}, headers=H, timeout=5)
algo_id = r.json().get('algoId', 0)
check(algo_id > 0, f'STOP_MARKET created: algoId={algo_id} @ {sl_price}')

# Also create TP
r = requests.post(f'{MOCK_HOST}/fapi/v1/algoOrder', data={
    'symbol': 'BTCUSDT', 'side': 'SELL', 'positionSide': 'LONG',
    'type': 'TAKE_PROFIT_MARKET', 'quantity': '0.05',
    'stopPrice': str(tp_price), 'workingType': 'MARK_PRICE',
    'timestamp': '1', 'signature': 'x'
}, headers=H, timeout=5)
check(r.json().get('algoId', 0) > 0, f'TAKE_PROFIT created @ {tp_price}')

# ═══════════════════════════════════════════
# 3. Verify position exists on exchange
# ═══════════════════════════════════════════
print('\n=== 3. Position verification ===')
r = requests.get(f'{MOCK_HOST}/fapi/v2/positionRisk', params={'timestamp': 1, 'signature': 'x'}, headers=H, timeout=5)
positions = [p for p in r.json() if abs(float(p.get('positionAmt', 0))) > 0]
check(len(positions) >= 1, f'position count: {len(positions)}')
btc = next((p for p in positions if p['symbol'] == 'BTCUSDT'), None)
check(btc is not None and btc['positionSide'] == 'LONG', 'BTCUSDT LONG exists')
check(float(btc['positionAmt']) > 0, f'qty: {btc["positionAmt"]}')

# ═══════════════════════════════════════════
# 4. Verify algo orders
# ═══════════════════════════════════════════
print('\n=== 4. Algo orders ===')
r = requests.get(f'{MOCK_HOST}/fapi/v1/openAlgoOrders', params={'timestamp': 1, 'signature': 'x'}, headers=H, timeout=5)
algos = r.json()
btc_algos = [a for a in algos if a.get('symbol') == 'BTCUSDT']
check(len(btc_algos) >= 1, f'BTCUSDT algo orders: {len(btc_algos)}')

# ═══════════════════════════════════════════
# 5. Simulate SL hit → close
# ═══════════════════════════════════════════
print('\n=== 5. Stop-loss trigger ===')
r = requests.post(f'{MOCK_HOST}/fapi/v1/order', data={
    'symbol': 'BTCUSDT', 'side': 'SELL', 'type': 'MARKET',
    'quantity': '0.1', 'positionSide': 'LONG',
    'timestamp': '1', 'signature': 'x'
}, headers=H, timeout=5)
check(r.json().get('status') == 'FILLED', 'SL close: FILLED')

# ═══════════════════════════════════════════
# 6. Verify position closed
# ═══════════════════════════════════════════
print('\n=== 6. Post-close verification ===')
r = requests.get(f'{MOCK_HOST}/fapi/v2/positionRisk', params={'timestamp': 1, 'signature': 'x'}, headers=H, timeout=5)
btc_pos = [p for p in r.json() if p['symbol'] == 'BTCUSDT' and abs(float(p.get('positionAmt', 0))) > 0]
check(len(btc_pos) == 0, f'BTCUSDT closed: {len(btc_pos)} remaining')

# ═══════════════════════════════════════════
# 7. Bidirectional: open LONG + SHORT same symbol
# ═══════════════════════════════════════════
print('\n=== 7. Hedge mode: LONG + SHORT ===')
requests.post(f'{MOCK_HOST}/fapi/v1/order', data={
    'symbol': 'ETHUSDT', 'side': 'BUY', 'type': 'MARKET',
    'quantity': '0.2', 'positionSide': 'LONG',
    'timestamp': '1', 'signature': 'x'
}, headers=H)
requests.post(f'{MOCK_HOST}/fapi/v1/order', data={
    'symbol': 'ETHUSDT', 'side': 'SELL', 'type': 'MARKET',
    'quantity': '0.1', 'positionSide': 'SHORT',
    'timestamp': '1', 'signature': 'x'
}, headers=H)
r = requests.get(f'{MOCK_HOST}/fapi/v2/positionRisk', params={'timestamp': 1, 'signature': 'x'}, headers=H, timeout=5)
eth = [p for p in r.json() if p['symbol'] == 'ETHUSDT']
check(len(eth) == 2, f'ETHUSDT dual: {len(eth)}')
sides = {p['positionSide'] for p in eth}
check('LONG' in sides and 'SHORT' in sides, f'both sides: {sides}')

# Close only LONG
requests.post(f'{MOCK_HOST}/fapi/v1/order', data={
    'symbol': 'ETHUSDT', 'side': 'SELL', 'type': 'MARKET',
    'quantity': '0.2', 'positionSide': 'LONG',
    'timestamp': '1', 'signature': 'x'
}, headers=H)
r = requests.get(f'{MOCK_HOST}/fapi/v2/positionRisk', params={'timestamp': 1, 'signature': 'x'}, headers=H, timeout=5)
eth = [p for p in r.json() if p['symbol'] == 'ETHUSDT']
check(len(eth) == 1 and eth[0]['positionSide'] == 'SHORT', 'SHORT survives LONG close')

# ═══════════════════════════════════════════
# 8. RiskEngine wired (import check)
# ═══════════════════════════════════════════
print('\n=== 8. Module verification ===')
sys.path.insert(0, '/opt/trading-bot/current/src')
modules = {
    'PositionKey': 'trading_bot.domain.position_key',
    'TradeType': 'trading_bot.domain.trade_type',
    'TradePlan': 'trading_bot.domain.trade_plan',
    'PositionSupervisor': 'trading_bot.execution.position_supervisor',
    'PositionSizeResult': 'trading_bot.execution.position_sizer',
    'InitialStopCalculator': 'trading_bot.strategy.stop_loss',
    'RiskEngine': 'trading_bot.risk.risk_engine',
    'ProtectionResult': 'trading_bot.exchange.protection',
}
for name, mod in modules.items():
    try:
        m = __import__(mod, fromlist=[name])
        check(hasattr(m, name), f'import {name}')
    except Exception as e:
        check(False, f'import {name}: {e}')

# ═══════════════════════════════════════════
# 9. No bare except:pass
# ═══════════════════════════════════════════
print('\n=== 9. Code quality ===')
r = subprocess.run(['grep', '-rn', 'except:', '/opt/trading-bot/current/src/trading_bot/', '--include=*.py'],
                   capture_output=True, text=True)
bare = [l for l in r.stdout.splitlines() if 'pass' in l and '__pycache__' not in l and '# ' not in l]
check(len(bare) == 0, f'bare except:pass = {len(bare)}')

print(f'\n{"="*50}\n{ok} passed, {fail} failed {"✅" if fail == 0 else "❌"}')
sys.exit(0 if fail == 0 else 1)

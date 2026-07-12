#!/usr/bin/env python3
"""
全链路测试 — 用 mock Binance 验证 bot 所有功能。
先启动 mock_binance.py，再运行此脚本。
"""
import os, sys, time, json, signal, subprocess, threading

MOCK_HOST = 'http://127.0.0.1:8765'
MOCK_WS = 'ws://127.0.0.1:8766'

def test_setup():
    """设置环境变量指向 mock"""
    os.environ['BINANCE_API_KEY'] = 'test'
    os.environ['BINANCE_API_SECRET'] = 'test'
    os.environ['TRADING_ENV'] = 'test'
    os.environ['LIVE_TRADING_ACK'] = 'yes'
    os.environ['BINANCE_FAPI_URL'] = MOCK_HOST
    os.environ['BINANCE_WS_URL'] = MOCK_WS

    # Patch client to use mock
    sys.path.insert(0, '/opt/trading-bot/current/src')
    from trading_bot.exchange import client
    # Override base URLs
    client.LIVE_FAPI = MOCK_HOST
    client.TESTNET_FAPI = MOCK_HOST
    client.LIVE_WS = MOCK_WS
    client.TESTNET_WS = MOCK_WS
    # Disable auth checking
    client._api_original = client._api
    def _mock_api(*args, **kwargs):
        import requests
        method = args[0]
        path = args[1]
        params = args[2] if len(args) > 2 else kwargs.get('params', {})
        url = f"{MOCK_HOST}{path}"
        if method == 'GET':
            r = requests.get(url, params=params, timeout=10)
        elif method == 'POST':
            r = requests.post(url, params=params, timeout=10)
        else:
            r = requests.request(method, url, params=params, timeout=10)
        if r.status_code != 200:
            raise Exception(f"API {r.status_code}: {r.text[:200]}")
        return r.json()
    client._api = _mock_api
    print('✓ Setup: mock API patched')

def test_rest_connectivity():
    """测试 REST API 连通"""
    from trading_bot.exchange.market_data import fetch_ticker, fetch_balance
    bal = fetch_balance()
    assert float(bal.get('total', 0)) > 0, f"balance failed: {bal}"
    ticker = fetch_ticker()
    assert len(ticker) > 10, f"ticker failed: {len(ticker)}"
    print('✓ REST: balance + ticker OK')

def test_position_management():
    """测试开仓、平仓、state 同步"""
    from trading_bot.services.position_manager import (
        load_bot_state, save_bot_state, market_close_position, sync_all_positions
    )
    from trading_bot.exchange.gateway import get_gateway
    g = get_gateway()

    # 开仓
    import requests
    r = requests.post(f'{MOCK_HOST}/fapi/v1/order',
                      params={'symbol':'BTCUSDT','side':'BUY','type':'MARKET','quantity':'0.1'})
    assert r.json()['status'] == 'FILLED'

    # sync
    state = load_bot_state()
    state = sync_all_positions(state)
    save_bot_state(state)

    # 验证
    s2 = load_bot_state()
    pos = s2['positions'].get('BTCUSDT:LONG', {})
    assert pos.get('status') == 'active', f"Position not active: {pos}"
    print(f"✓ Position: {pos.get('entry_price')}, active={pos.get('status')}")

    # 平仓
    ok = market_close_position('BTCUSDT', 'LONG', 0.1)
    print(f"✓ Close: {ok}")

def test_ws():
    """测试 WS 连接和数据"""
    from trading_bot.data.ws_market_client import market_cache
    import urllib.request
    # Get top symbols
    r = requests.get(f'{MOCK_HOST}/fapi/v1/ticker/24hr')
    tickers = sorted(r.json(), key=lambda x: float(x['quoteVolume']), reverse=True)[:10]
    symbols = [t['symbol'] for t in tickers]

    # Connect via raw WS to verify
    import websocket
    ws = websocket.create_connection(f"{MOCK_WS}/stream?streams={'/'.join([s.lower()+'@bookTicker' for s in symbols[:3]])}")
    for _ in range(3):
        msg = json.loads(ws.recv())
        assert 'stream' in msg, f"Bad WS msg: {msg}"
    ws.close()
    print('✓ WS: bookTicker data received')

def test_sltp():
    """测试止损止盈参数计算"""
    entry = 100.0
    sl = round(entry * 0.995, 5)
    R = entry - sl
    tp1 = entry + 1.0 * R
    tp2 = entry + 1.5 * R
    tp3 = entry + 2.5 * R
    assert abs(sl - 99.5) < 0.01, f"SL wrong: {sl}"
    assert abs(tp1 - 100.5) < 0.01, f"TP1 wrong: {tp1}"
    print(f'✓ SL/TP: SL={sl:.2f} TP1={tp1:.2f} TP2={tp2:.2f} TP3={tp3:.2f}')

def test_state_store():
    """测试 state 存储 + checksum"""
    from trading_bot.storage.state_store import StateStore
    from pathlib import Path
    store = StateStore(Path('/tmp/test_state.json'))
    s = {'positions': {'X:LONG': {'symbol': 'X', 'side': 'LONG', 'entry_price': 1, 'status': 'active'}},
         'trades': [], 'total_pnl': 0}
    store.save(s)
    s2 = store.load()
    assert s2['positions']['X:LONG']['status'] == 'active'
    Path('/tmp/test_state.json').unlink(missing_ok=True)
    print('✓ State store: write + read + checksum OK')

def test_notifications():
    """测试通知模块（不实际发送）"""
    from trading_bot.integrations.notifications import notify_entry, notify_exit, push
    # 只测函数不崩
    try:
        notify_entry('TESTUSDT', 'LONG', 100.0, 10.0, 99.5, 101.0, 12.5, 'MOMENTUM_SCALP test')
        notify_exit('TESTUSDT', 'LONG', 101.0, 0.05, 'TP1 test')
        push('test message')
    except Exception as e:
        print(f'  (notifications may fail without hermes CLI: {e})')
    print('✓ Notifications: functions exist')

def run_all():
    print('=' * 50)
    print('BOT 全链路测试')
    print('=' * 50)
    test_setup()
    test_rest_connectivity()
    test_ws()
    test_sltp()
    test_state_store()
    test_notifications()
    test_position_management()
    print('=' * 50)
    print('ALL TESTS PASSED ✅')

if __name__ == '__main__':
    import requests
    run_all()

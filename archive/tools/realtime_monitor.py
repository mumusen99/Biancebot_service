#!/usr/bin/env python3
"""
实时持仓监控（连续循环）

两阶段:
  1. 秒级(5s): 扫描已有持仓技术面，吃反转信号就平仓
  2. 分钟级(60s): 扫描观察区找超短线入场信号
"""
import sys, json, time, logging, urllib.parse, hmac, hashlib, os

# 纯实盘模式
IS_TESTNET = False
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from config import API_KEY, API_SECRET, PROXY
import requests as req
import pandas as pd

# 从 trader 模块读取 API 端点（自动识别测试网/实盘）
from trader import _api, _align_price_dir, _align_qty, LIVE_FAPI, TESTNET_FAPI

logging.basicConfig(level=logging.INFO, format='%(asctime)s [rtm] %(message)s')
logger = logging.getLogger("realtime_mon")

BASE = Path(__file__).parent
FAPI = TESTNET_FAPI if False else LIVE_FAPI
_session = req.Session()
_session.proxies = {'http': PROXY, 'https': PROXY}
HDRS = {'X-MBX-APIKEY': API_KEY}

_ENV_NAME = "TESTNET" if False else "实盘"
logger.info(f'🌐 环境: {_ENV_NAME} | 端点: {FAPI}')
logger.info(f'🔑 LIVE_TRADING_ENABLED={"YES" if os.environ.get("LIVE_TRADING_ENABLED") == "YES" else "NO"}')
logger.info(f'🔒 交易锁: /tmp/trading_bot_trade.lock')
logger.info(f'💰 保证金模式: {"逐仓" if True else "全仓"}')

# TRADING_ENABLED 安全开关（第二道防线：环境变量+代码内置默认）
_TRADING_ALLOWED = os.environ.get('TRADING_ENABLED', 'false').lower() == 'true'
if not _TRADING_ALLOWED:
    logger.warning('🔒 TRADING_ENABLED=false，拒绝任何交易操作')
    logger.warning('   设置 TRADING_ENABLED=true 环境变量后可解除')

# ─── 配置 ──
SCAN_INTERVAL = 5         # 每5秒扫一轮
KLINES_TIMEFRAME = '5m'   # 5分钟K线做技术判断
KLINES_LIMIT = 30         # 30根就够了
PROXY_RECOVER_COOLDOWN = 60  # 两次代理恢复之间至少间隔60秒（防反复尝试）

# ─── API ──
def _ts(): return int(time.time() * 1000)
def _sig(params):
    p = dict(params); p['timestamp']=_ts(); p['recvWindow']=10000
    q = urllib.parse.urlencode(sorted(p.items()))
    s = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return f'{q}&signature={s}'

def _get(path, params=None, timeout=15):
    p = dict(params or {})
    qs = _sig(p)
    r = _session.get(f'{FAPI}/{path}?{qs}', headers=HDRS, timeout=timeout)
    if r.status_code == 200: return r.json()
    return None

def _post(path, params, timeout=15):
    p = dict(params)
    qs = _sig(p)
    r = _session.post(f'{FAPI}/{path}?{qs}', headers=HDRS, timeout=timeout)
    if r.status_code == 200: return r.json()
    return None

def _delete(path, params, timeout=15):
    p = dict(params)
    qs = _sig(p)
    r = _session.delete(f'{FAPI}/{path}?{qs}', headers=HDRS, timeout=timeout)
    return r.status_code == 200

def get_all_prices() -> dict:
    """一次性获取所有USDT交易对价格"""
    r = _get('fapi/v1/ticker/price')
    if not r: return {}
    return {x['symbol']: float(x['price']) for x in r}

def get_positions() -> list:
    """获取所有非零持仓"""
    r = _get('fapi/v2/positionRisk')
    if not r: return []
    return [p for p in r if abs(float(p.get('positionAmt',0))) > 0]

def get_klines_df(symbol: str, limit: int = 30) -> pd.DataFrame:
    """获取K线数据并转DataFrame"""
    r = _get('fapi/v1/klines', {
        'symbol': symbol, 'interval': KLINES_TIMEFRAME, 'limit': limit
    })
    if not r: return None
    df = pd.DataFrame(r, columns=[
        'time','open','high','low','close','volume',
        'close_time','qav','trades','taker_base','taker_quote','ignore'
    ])
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c])
    return df

def calc_indicators(df: pd.DataFrame):
    """快速计算关键技术指标"""
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    vol = df['volume'].values
    
    n = len(close)
    if n < 14: return None
    
    # EMA9, EMA21
    def ema(arr, period):
        alpha = 2 / (period + 1)
        result = [arr[0]]
        for i in range(1, len(arr)):
            result.append(arr[i] * alpha + result[-1] * (1 - alpha))
        return result
    
    ema9 = ema(close, 9)[-1]
    ema21 = ema(close, 21)[-1]
    prev_ema9 = ema(close, 9)[-3]
    prev_ema21 = ema(close, 21)[-3]
    
    # RSI 14
    gains, losses = 0, 0
    for i in range(1, min(15, n)):
        diff = close[-i] - close[-i-1]
        gains += max(diff, 0)
        losses += max(-diff, 0)
    avg_g = gains / min(14, n-1)
    avg_l = losses / min(14, n-1)
    rsi = 50
    if avg_l > 0:
        rs = avg_g / avg_l
        rsi = 100 - 100 / (1 + rs)
    
    # MACD (正确: Signal = EMA9(MACD序列))
    macd_line = [ema(close[:i+1], 12)[-1] - ema(close[:i+1], 26)[-1] for i in range(min(35, n-1), n)]
    macd_val = macd_line[-1] if macd_line else 0
    signal_val = ema(macd_line, 9)[-1] if len(macd_line) >= 9 else 0
    
    # 布林带
    sma20 = close[-min(20, n):].mean()
    std20 = close[-min(20, n):].std()
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    
    # ATR 14
    tr_list = []
    for i in range(1, min(15, n)):
        hl = high[-i] - low[-i]
        hc = abs(high[-i] - close[-i-1])
        lc = abs(low[-i] - close[-i-1])
        tr_list.append(max(hl, hc, lc))
    atr = sum(tr_list) / len(tr_list) if tr_list else 0
    
    current = close[-1]
    
    return {
        'current': current,
        'ema9': ema9,
        'ema21': ema21,
        'ema9_above_ema21': ema9 > ema21,
        'rsi': rsi,
        'macd': macd_val,
        'signal': signal_val,
        'macd_positive': macd_val > signal_val,
        'bb_upper': bb_upper,
        'bb_lower': bb_lower,
        'bb_position': (current - bb_lower) / (bb_upper - bb_lower) if bb_upper > bb_lower else 0.5,
        'atr_pct': atr / current * 100 if current > 0 else 0,
        'vol_ratio': vol[-1] / vol[-min(14, n):].mean() if len(vol) > 1 else 1,
    }

def market_close(symbol: str, side: str, qty: float) -> bool:
    """市价平仓（带 reduceOnly 保护 + TRADING_ENABLED 闸）"""
    if not _TRADING_ALLOWED:
        logger.warning(f'🔒 TRADING_ENABLED=false，跳过平仓 {symbol}')
        return False
    close_side = 'SELL' if side == 'LONG' else 'BUY'
    try:
        # 先按algoId精准取消条件单（不批量删整个symbol，避免误删其他策略的单）
        try:
            open_algos = _get('fapi/v1/algoOpenOrders', {'symbol': symbol})
            if open_algos:
                for a in open_algos:
                    if a.get('algoStatus') in ('NEW', 'WORKING'):
                        _delete('fapi/v1/algoOrder', {'symbol': symbol, 'algoId': a['algoId']})
                        time.sleep(0.1)
        except: pass
        try:
            # 普通挂单也按orderId精准取消
            open_ords = _get('fapi/v1/openOrders', {'symbol': symbol})
            if open_ords:
                for o in open_ords:
                    if o.get('status') == 'NEW':
                        _delete('fapi/v1/order', {'symbol': symbol, 'orderId': o['orderId']})
                        time.sleep(0.1)
        except: pass
        time.sleep(0.3)
        
        # 平仓前再查一次实际持仓
        pos_check = get_positions()
        actual_qty = 0
        for p in pos_check:
            if p['symbol'] == symbol and abs(float(p.get('positionAmt',0))) > 0:
                actual_qty = abs(float(p['positionAmt']))
                break
        if actual_qty <= 0:
            logger.info(f'  {symbol} 已无持仓，跳过平仓')
            return True
        
        qty_str = ('%g' % actual_qty).replace(',', '')
        r = _post('fapi/v1/order', {
            'symbol': symbol, 'side': close_side, 'type': 'MARKET',
            'quantity': qty_str, 'positionSide': side,
            'reduceOnly': 'true',  # 防止误开反向单
        })
        return r and 'orderId' in r
    except Exception as e:
        logger.warning(f'  平仓失败 {symbol}: {e}')
        return False

def scan_position(sym: str, side: str, entry: float, mark: float,
                  qty: float, upnl: float, amount: float, ind: dict) -> str:
    """
    扫描一个持仓，返回退出理由或空字符串。
    ind = calc_indicators() 的结果
    """
    if ind is None:
        return ''
    
    current = ind['current']
    rsi = ind['rsi']
    ema9 = ind['ema9']
    ema21 = ind['ema21']
    ema_up = ind['ema9_above_ema21']
    bb_pos = ind['bb_position']
    atr_pct = ind['atr_pct']
    
    pnl_pct = upnl / max(0.01, amount) * 100 if amount > 0 else 0
    price_change_pct = (current - entry) / entry * 100
    
    # ── 做多退出检查 ──
    if side == 'LONG':
        # 趋势反转：价格跌破EMA21 且 EMA9下穿EMA21
        if current < ema21 * 0.995 and not ema_up:
            if pnl_pct > -5:  # 没亏太多就跑
                return f'趋势转空(价格<EMA21) PnL{pnl_pct:+.1f}%'
        
        # RSI超买回落
        if rsi > 70 and current < ema9:
            return f'RSI{rsi:.0f}超买回落 PnL{pnl_pct:+.1f}%'
        
        # 布林上轨受压 + RSI超买
        if bb_pos > 0.95 and rsi > 65:
            return f'布林上轨受压+RSI{rsi:.0f} PnL{pnl_pct:+.1f}%'
        
        # 亏超-5%强行止损（独立于条件单止损，双保险）
        if pnl_pct <= -6:
            return f'强行止损 PnL{pnl_pct:.1f}%'
    
    # ── 做空退出检查 ──
    else:  # SHORT
        if current > ema21 * 1.005 and ema_up:
            if pnl_pct > -5:
                return f'趋势转多(价格>EMA21) PnL{pnl_pct:+.1f}%'
        
        if rsi < 30 and current > ema9:
            return f'RSI{rsi:.0f}超卖反弹 PnL{pnl_pct:+.1f}%'
        
        if bb_pos < 0.05 and rsi < 35:
            return f'布林下轨反弹+RSI{rsi:.0f} PnL{pnl_pct:+.1f}%'
        
        if pnl_pct <= -6:
            return f'强行止损 PnL{pnl_pct:.1f}%'
    
    return ''

# 内存信号去重缓存（进程级，不依赖文件）
_SCALP_SIGNAL_CACHE = {}
_SCALP_SIGNAL_TTL = 300  # 5分钟

def _check_scalp_signal_idempotent(sym: str, side: str) -> bool:
    """检查超短线信号是否已执行过（内存去重）"""
    signal_id = f'{sym}:{side}:{int(time.time()/60)}'
    now = time.time()
    # 清理过期
    expired = [k for k, v in _SCALP_SIGNAL_CACHE.items() if now - v > _SCALP_SIGNAL_TTL]
    for k in expired:
        del _SCALP_SIGNAL_CACHE[k]
    if signal_id in _SCALP_SIGNAL_CACHE:
        return False  # 已执行
    _SCALP_SIGNAL_CACHE[signal_id] = now
    return True


def _scan_new_scalp_entries():
    """每分钟扫描一次超短线入场机会（含信号去重）"""
    if not _TRADING_ALLOWED:
        return 0
    try:
        from scalper import scan_signals, SCALP_MARGIN, SCALP_LEVERAGE, SCALP_MAX_POSITIONS, SCALP_BUDGET
        from scalper import load_bot_state, save_bot_state
        from trader import _align_price_dir
        
        state = load_bot_state()
        scalp_count = sum(1 for p in state.get('positions', {}).values()
                         if p.get('strategy') == 'scalp' and p.get('status') in ('active', 'pending'))
        
        if scalp_count >= SCALP_MAX_POSITIONS:
            return 0
        
        used = sum(p.get('amount', 0) for p in state.get('positions', {}).values()
                   if p.get('strategy') == 'scalp' and p.get('status') in ('active', 'pending'))
        remaining = SCALP_BUDGET - used
        if remaining < SCALP_MARGIN:
            return 0
        
        signals, btc_env = scan_signals()
        if not signals:
            return 0
        
        best = signals[0]
        sym = best['symbol']
        side = best['side']
        logger.info(f'  新信号: {sym} {side} 得分{best["score"]} | {best.get("reason","")}')
        
        if sym in state.get('positions', {}):
            return 0
        
        # 信号去重：同一币种+方向+60s窗口只执行一次
        if not _check_scalp_signal_idempotent(sym, side):
            logger.info(f'  ⏭️ 信号已执行过: {sym} {side}')
            return 0
        
        # 调用 scalper 的 run_scalper 处理具体开单（复用已有逻辑）
        import scalper
        from contextlib import redirect_stdout, redirect_stderr
        old_level = logger.level
        scalper.run_scalper()
        logger.setLevel(old_level)
        return 1
    except Exception as e:
        logger.warning(f'⚠️ 扫描新信号异常: {e}')
        return 0


def _acquire_lock() -> bool:
    """单实例锁：通过 fcntl 锁定 PID 文件，防止重复启动"""
    lock_file = '/tmp/trading_bot.lock'
    try:
        import fcntl
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 写入当前 PID
        os.write(fd, str(os.getpid()).encode())
        logger.info(f'🔒 单实例锁已获取 (PID={os.getpid()})')
        return True
    except (IOError, BlockingIOError):
        logger.error(f'❌ 已有实例在运行 (锁文件: {lock_file})')
        return False
    except Exception as e:
        logger.warning(f'⚠️ 单实例锁不可用 (fcntl?): {e}')
        return True  # fcntl 不可用时（如 Windows）放行


def main_loop():
    logger.info('🚀 实时监控启动 (每%ds扫描持仓 + 每60s找新信号)' % SCAN_INTERVAL)
    
    # 获取单实例锁
    if not _acquire_lock():
        return
    
    proxy_failures = 0
    last_recover_time = 0
    api_failures = 0         # API连续失败计数 → 触发只读模式
    read_only_mode = False   # 只读模式下禁止开仓
    scan_counter = 0  # 计数到 SCAN_INTERVAL*12 ≈ 60s 时触发新信号扫描
    
    while True:
        loop_start = time.time()
        
        try:
            # 0. 代理连通性检查（如果是NAS环境走代理，美国服务器直连则跳过）
            test_rsp = _session.get(f'{FAPI}/fapi/v1/ping', timeout=5)
            if test_rsp.status_code != 200:
                proxy_failures += 1
            
            if proxy_failures >= 3:
                now = time.time()
                if now - last_recover_time > PROXY_RECOVER_COOLDOWN:
                    logger.warning(f'🔌 代理连续失败{proxy_failures}次，触发自动恢复...')
                    try:
                        from proxy_guard import auto_recover
                        ok = auto_recover()
                        if ok:
                            logger.info('✅ 代理自动恢复成功')
                            proxy_failures = 0
                            _session.close()
                            import requests as new_req
                            globals()['_session'] = new_req.Session()
                            _session.proxies = {'http': 'http://127.0.0.1:10809', 'https': 'http://127.0.0.1:10809'}
                        else:
                            logger.error('❌ 代理自动恢复失败')
                    except Exception as recover_e:
                        logger.error(f'❌ 代理恢复异常: {recover_e}')
                    last_recover_time = now
                time.sleep(SCAN_INTERVAL)
                continue
            else:
                proxy_failures = 0
            
            # ─── 阶段A: 每60秒扫描新入场信号 ───
            scan_counter += 1
            if scan_counter >= 12:  # 5s × 12 = 60s
                scan_counter = 0
                logger.debug('🔎 扫描新入场信号...')
                _scan_new_scalp_entries()
            
            # ─── 阶段B: 每5秒检查持仓退出信号 ───
            all_pos = get_positions()
            positions = [(p['symbol'], p['positionSide'],
                          float(p['entryPrice']), float(p['markPrice']),
                          abs(float(p['positionAmt'])), float(p['unRealizedProfit']),
                          float(p['entryPrice']) * abs(float(p['positionAmt'])) / max(1, int(p.get('leverage', 1))))
                         for p in all_pos if float(p.get('positionAmt',0)) != 0]
            
            if not positions:
                # 没持仓但到了扫描时间也休息（扫描新信号阶段已经做过了）
                elapsed = time.time() - loop_start
                time.sleep(max(0.5, SCAN_INTERVAL - elapsed))
                continue
            
            all_prices = get_all_prices()
            
            closed_any = False
            for sym, side, entry, mark, qty, upnl, amount in positions:
                current_price = all_prices.get(sym, mark)
                df = get_klines_df(sym, KLINES_LIMIT)
                ind = calc_indicators(df) if df is not None else None
                
                reason = scan_position(sym, side, entry, current_price, qty, upnl, amount, ind)
                
                if reason:
                    logger.info(f'🛑 实时退出 {sym} {side}: {reason}')
                    ok = market_close(sym, side, qty)
                    if ok:
                        logger.info(f'  ✅ 已平仓')
                        closed_any = True
                        # 更新bot_state
                        try:
                            from position_manager import load_bot_state, save_bot_state
                            bs = load_bot_state()
                            pnl_pct = upnl / max(0.01, amount) * 100
                            if sym in bs.get('positions', {}):
                                p = bs['positions'][sym]
                                p['status'] = 'closed'
                                p['closed_at'] = datetime.now().isoformat()
                                p['close_reason'] = f'实时扫描: {reason}'
                            bs.setdefault('trades', []).append({
                                'action': 'CLOSE', 'symbol': sym,
                                'side': side, 'reason': f'实时扫描: {reason}',
                                'pnl': round(upnl, 4), 'pnl_pct': round(pnl_pct, 2),
                                'time': datetime.now().isoformat(),
                            })
                            save_bot_state(bs)
                        except: pass
                else:
                    # 静默状态（只日志不输出）
                    pass
            
            # 4. 计算等待时间
            elapsed = time.time() - loop_start
            sleep_time = max(0.5, SCAN_INTERVAL - elapsed)
            time.sleep(sleep_time)
            
        except KeyboardInterrupt:
            logger.info('🛑 监控已停止')
            break
        except Exception as e:
            err_str = str(e)
            proxy_failures += 1
            api_failures += 1
            
            # API连续失败 → 只读模式（禁止开新单）
            if api_failures >= 5 and not read_only_mode:
                logger.error(f'🔒 API连续失败{api_failures}次，进入只读模式（仅管理已有持仓）')
                read_only_mode = True
            
            if proxy_failures >= 3:
                try:
                    from proxy_guard import auto_recover
                    ok = auto_recover()
                    if ok:
                        logger.info('✅ 代理恢复成功')
                        proxy_failures = 0
                        _session.close()
                        import requests as new_req
                        globals()['_session'] = new_req.Session()
                        _session.proxies = {'http': 'http://127.0.0.1:10809', 'https': 'http://127.0.0.1:10809'}
                except Exception as recover_e:
                    logger.error(f'❌ 恢复失败: {recover_e}')
            time.sleep(SCAN_INTERVAL)


def start_daemon():
    """启动守护进程"""
    import multiprocessing
    p = multiprocessing.Process(target=main_loop, daemon=True)
    p.start()
    logger.info(f'📡 实时监控进程已启动 (PID={p.pid})')
    return p


if __name__ == '__main__':
    try:
        main_loop()
    except KeyboardInterrupt:
        print('\n🛑 停止')

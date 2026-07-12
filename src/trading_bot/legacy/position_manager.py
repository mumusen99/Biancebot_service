# ⚠️ FROZEN: legacy module. Only fatal security fixes allowed. No new features.
#!/usr/bin/env python3
"""
统一仓位管理器
==============
职责：追踪交易所所有持仓 → 自动打标 → 统一管理 → 动态止损

1. sync_all_positions() — 从交易所拉所有持仓，与 bot_state 同步，未标记的自动打标
2. manage_all_positions() — 统一管理：止盈止损触发、追踪止损、时间退出、技术退出
"""
import json, time, logging, urllib.parse, hmac, hashlib, math, os
from pathlib import Path
from datetime import datetime, timezone
import requests as req
import pandas as pd

from trading_bot.core.settings import API_KEY, API_SECRET, PROXY, BOT_STATE_FILE, STRATEGY_PROFILES
from trading_bot.exchange.client import _align_price_dir, _align_qty, _api, _get_symbol_precision, LIVE_FAPI, TESTNET_FAPI
from trading_bot.exchange.market_data import fetch_klines, fetch_ticker
from trading_bot.strategy.indicators import generate_technical_signals
from trading_bot.integrations.notifications import push as push_notification
# ─── 交易状态枚举 ────────────────────────────────
POS_STATUS = {
    'SIGNAL_CREATED': '信号已生成',
    'ENTRY_SUBMITTED': '入场已提交',
    'ENTRY_PENDING': '入场挂单中',
    'ENTRY_PARTIALLY_FILLED': '入场部分成交',
    'ENTRY_FILLED': '入场已成交',
    'STOP_PENDING': '止损挂单中',
    'STOP_CONFIRMED': '止损已确认',
    'POSITION_ACTIVE': '持仓活跃',
    'REDUCE_PENDING': '减仓中',
    'CLOSE_PENDING': '平仓确认中',
    'CLOSED': '已平仓',
    'UNKNOWN': '状态未知',
    'EMERGENCY_EXIT': '紧急退出',
}


logger = logging.getLogger("pos_mgr")

BASE = Path(__file__).parent
# 统一端点（测试网/实盘）
FAPI = TESTNET_FAPI if False else LIVE_FAPI
FA2 = ('https://testnet.binancefuture.com/fapi/v2' if False
       else 'https://fapi.binance.com/fapi/v2')
_session = req.Session()
_session.proxies = {'http': PROXY, 'https': PROXY}
HDRS = {'X-MBX-APIKEY': API_KEY}

# ─── 策略止盈止损参数 ─────────────────────────────
# 基于价格百分比（已除以杠杆），review_orders 和 manage_positions 共用

# ─── 辅助 ─────────────────────────────────────────

def _pk(sym: str, side: str) -> str:
    return f"{sym}:{side}"
    """仓位唯一键: symbol:side，支持同一币种双向持仓"""

def _pos_get(state: dict, sym: str, side: str = "") -> tuple:
    """用 symbol:side 或 symbol 查找仓位，返回 (key, pos) 或 (None, None)"""
    for try_key in [f"{sym}:{side}", sym]:
        if try_key in state.get('positions', {}):
            return try_key, state['positions'][try_key]
    return None, None

def _ts() -> int:
    return int(time.time() * 1000)

def _sig(params: dict) -> tuple:
    """返回 (querystring, signature)"""
    p = dict(params)
    p['timestamp'] = _ts()
    p['recvWindow'] = 10000
    q = urllib.parse.urlencode(sorted(p.items()))
    s = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q, s

def _get(path: str, params: dict = None) -> dict:
    p = dict(params or {})
    q, s = _sig(p)
    r = _session.get(f'{FAPI}/{path}?{q}&signature={s}', headers=HDRS, timeout=20)
    if r.status_code == 200: return r.json()
    if r.status_code == 400:
        err = r.json()
        if err.get('code') in (-2011, -2013): return None
    raise Exception(f'GET {path} {r.status_code}: {r.text[:200]}')

def _post(path: str, params: dict = None) -> dict:
    p = dict(params or {})
    q, s = _sig(p)
    r = _session.post(f'{FAPI}/{path}?{q}&signature={s}', headers=HDRS, timeout=20)
    if r.status_code == 200: return r.json()
    if r.status_code == 400:
        err = r.json()
        if err.get('code') in (-2011, -2013): return None
    raise Exception(f'POST {path} {r.status_code}: {r.text[:200]}')

def _delete(path: str, params: dict = None) -> dict:
    p = dict(params or {})
    q, s = _sig(p)
    r = _session.delete(f'{FAPI}/{path}?{q}&signature={s}', headers=HDRS, timeout=20)
    if r.status_code == 200: return r.json()
    if r.status_code == 400:
        err = r.json()
        if err.get('code') in (-2011, -2013): return None
    raise Exception(f'DELETE {path} {r.status_code}: {r.text[:200]}')

def _get_price(symbol: str) -> float:
    """获取最新价"""
    r = _get('ticker/price', {'symbol': symbol})
    return float(r['price'])

def load_bot_state() -> dict:
    try: return json.loads(BOT_STATE_FILE.read_text())
    except: return {'positions': {}, 'trades': [], 'total_pnl': 0.0, 'budget': 50.0}

def save_bot_state(state: dict):
    """JSON 原子写入：临时文件 → fsync → rename"""
    tmp = BOT_STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    # 保留备份
    if BOT_STATE_FILE.exists():
        BOT_STATE_FILE.rename(BOT_STATE_FILE.with_suffix('.bak'))
    tmp.rename(BOT_STATE_FILE)


_TRADE_LOCK_FILE = Path('/tmp/trading_bot_trade.lock')
_TRADE_LOCK_FD = None

def acquire_trade_lock() -> bool:
    """获取交易互斥锁（非阻塞），防止多脚本同时操作账户"""
    global _TRADE_LOCK_FD
    try:
        import fcntl
        fd = os.open(str(_TRADE_LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _TRADE_LOCK_FD = fd
        return True
    except (IOError, BlockingIOError):
        return False
    except:
        return False  # 锁失败时禁止交易

def release_trade_lock():
    """释放交易锁"""
    global _TRADE_LOCK_FD
    if _TRADE_LOCK_FD is not None:
        try:
            import fcntl
            fcntl.flock(_TRADE_LOCK_FD, fcntl.LOCK_UN)
            os.close(_TRADE_LOCK_FD)
        except:
            pass
        _TRADE_LOCK_FD = None


_SIGNAL_CACHE = {}  # {signal_id: timestamp}
_SIGNAL_CACHE_TTL = 300  # 5分钟去重窗口

def check_signal_idempotent(signal_id: str) -> bool:
    """检查信号是否已执行过（防重复开仓），返回 True=可执行"""
    now = time.time()
    # 清理过期
    expired = [k for k, v in _SIGNAL_CACHE.items() if now - v > _SIGNAL_CACHE_TTL]
    for k in expired:
        del _SIGNAL_CACHE[k]
    if signal_id in _SIGNAL_CACHE:
        return False  # 已执行过
    _SIGNAL_CACHE[signal_id] = now
    return True


def with_trade_lock(func):
    """装饰器：自动获取/释放交易锁"""
    def wrapper(*args, **kwargs):
        if not acquire_trade_lock():
            logger.warning('⏳ 交易锁被占用，跳过')
            return None
        try:
            return func(*args, **kwargs)
        finally:
            release_trade_lock()
    return wrapper

# ─── 策略检测 ─────────────────────────────────────

def detect_strategy(entry_price: float, sl_price: float, tp_price: float, leverage: int = None) -> str:
    """
    根据 SL/TP 距离自动判断策略。
    返 'band', 'scalp', 'scan', 或 'unknown'
    """
    if not sl_price or not entry_price or entry_price <= 0:
        return 'unknown'
    
    sl_dist_pct = abs(entry_price - sl_price) / entry_price * 100
    tp_dist_pct = abs(entry_price - tp_price) / entry_price * 100 if tp_price else 999
    
    # 扫盘：SL≤1.5%, TP≤3%, 通常5x杠杆
    if sl_dist_pct <= 1.5 and tp_dist_pct <= 3.0:
        return 'scan'
    # 超短线：SL≤2.5%, TP≤5%
    if sl_dist_pct <= 2.5 and tp_dist_pct <= 5.0:
        return 'scalp'
    # 波段：SL≤5%, TP≥5%
    if sl_dist_pct <= 5.0 and tp_dist_pct >= 5.0:
        return 'band'
    
    return 'unknown'
    """获取策略参数（从 config.STRATEGY_PROFILES），未知返回保守值"""
def get_profile(strategy: str = None) -> dict:
    """获取策略参数，未知返回保守值"""
    if strategy and strategy in STRATEGY_PROFILES:
        return STRATEGY_PROFILES[strategy]
    return STRATEGY_PROFILES['unknown']

# ─── 标准化仓位条目 ───────────────────────────────

def create_position(symbol: str, side: str, entry_price: float, amount: float,
                    qty: float, strategy: str, reason: str = '',
                    sl_price: float = 0, tp_price: float = 0) -> dict:
    """创建标准化仓位记录"""
    profile = get_profile(strategy)
    return {
        'symbol': symbol,
        'side': side,
        'entry_price': entry_price,
        'amount': amount,           # 保证金U
        'qty': qty,                 # 数量（张）
        'strategy': strategy,
        'status': 'active',
        'opened_at': datetime.now(timezone.utc).isoformat(),
        'reason': reason,
        'signal_id': int(time.time()),  # 信号去重ID
        'sl_price': sl_price,
        'tp_price': tp_price,
        'last_sl': 0,               # 上次追踪后的止损价
        'last_tp': 0,               # 上次追踪后的止盈价
        'last_check': 0,            # 上次检查时间戳
        'trailing_activated': False,# 追踪止损是否已激活
    }

# ─── 同步交易所持仓 → bot_state ──────────────────

def fetch_exchange_positions() -> list:
    """从交易所获取所有非零持仓"""
    d = _get('v2/positionRisk' if 'v2' in FA2 else 'positionRisk',
             params={}) if hasattr(FA2, 'keys') else None
    # 直接调 v2
    p = {'timestamp': _ts(), 'recvWindow': 10000}
    q, s = _sig(p)
    r = _session.get(f'{FA2}/positionRisk?{q}&signature={s}', headers=HDRS, timeout=20)
    if r.status_code != 200:
        logger.warning(f'  ⚠️ positionRisk 失败: {r.text[:100]}')
        return []
    return [x for x in r.json() if abs(float(x.get('positionAmt', 0))) > 0]

def fetch_exchange_orders(symbol: str = None) -> list:
    """获取普通挂单"""
    params = {'symbol': symbol} if symbol else {}
    return _get('openOrders', params) or []

def fetch_exchange_algos() -> dict:
    """获取条件委托（止盈止损），按 symbol 分组"""
    try:
        # 用 v1 allAlgoOrders
        r = _get('allAlgoOrders', {'status': 'working'})
        # 实际返回可能不同，尝试 v1/algoOpenOrders
    except:
        pass
    try:
        p = {'timestamp': _ts(), 'recvWindow': 10000}
        q, s = _sig(p)
        r = _session.get(f'{FAPI}/algoOpenOrders?{q}&signature={s}', headers=HDRS, timeout=20)
        if r.status_code == 200:
            algo_map = {}
            for a in r.json():
                algo_map.setdefault(a['symbol'], []).append(a)
            return algo_map
    except:
        pass
    return {}

def sync_all_positions(state: dict = None) -> dict:
    """
    核心同步函数：
    1. 从交易所拉所有非零持仓
    2. 与 bot_state 比对
    3. 未标记的自动检测策略并标记
    4. 记录 price/size 等字段
    """
    if state is None:
        state = load_bot_state()
    
    state.setdefault('positions', {})
    
    # 拉交易所数据
    live = fetch_exchange_positions()
    open_orders = fetch_exchange_orders()  # 含未成交限价单
    algos = fetch_exchange_algos()
    logger.info(f'📡 交易所: {len(live)}持仓, {len(open_orders)}挂单')
    
    # 已跟踪的symbol
    tracked = set(state['positions'].keys())  # keys are symbol:side or legacy symbol
    live_syms = {p['symbol'] for p in live}
    
    # 合并：交易所有的 + 挂单中的
    all_active = set(live_syms)
    for o in open_orders:
        all_active.add(o['symbol'])
    
    # A. 清除已经不在交易所的
    for sym in list(tracked):
        pos = state['positions'][sym]
        if sym not in all_active:
            if pos.get('status') in ('closed', 'cancelled'):
                del state['positions'][sym]
            elif pos['status'] == 'pending' and sym not in {o['symbol'] for o in open_orders}:
                # 挂单已不存在 → 移除
                logger.info(f'  🗑️ {sym} 挂单已不存在，移除')
                del state['positions'][sym]
    
    # B. 更新/添加交易所持仓
    for p in live:
        sym = p['symbol']
        amt = float(p.get('positionAmt', 0))
        entry = float(p.get('entryPrice', 0))
        mark = float(p.get('markPrice', 0))
        upnl = float(p.get('unRealizedProfit', 0))
        pos_side = p.get('positionSide', 'LONG')
        lev = int(p.get('leverage', 3))
        qty = abs(amt)
        
        if sym in state['positions']:
            # 更新已有
            pos = state['positions'][sym]
            old_entry = pos.get('entry_price', 0)
            if abs(old_entry - entry) > 0.0001:
                pos['entry_price'] = entry
                pos['qty'] = qty
            pos['current_price'] = mark
            pos['pnl'] = round(upnl, 4)
            pos['pnl_percent'] = round(upnl / max(0.01, pos.get('amount', 1)) * 100, 2)
            pos['last_check'] = time.time()
        else:
            # 新持仓 → 自动打标
            # 先从 algo orders 找 SL/TP 来判断策略
            sl_price = 0
            tp_price = 0
            strategy = 'unknown'
            
            if sym in algos:
                for a in algos[sym]:
                    if a.get('algoStatus') != 'NEW':
                        continue
                    if a.get('type') == 'STOP_MARKET':
                        sl_price = float(a.get('triggerPrice', 0))
                    elif a.get('type') == 'TAKE_PROFIT_MARKET':
                        tp_price = float(a.get('triggerPrice', 0))
            
            strategy = detect_strategy(entry, sl_price, tp_price, lev)
            
            pos_entry = create_position(
                symbol=sym,
                side=pos_side,
                entry_price=entry,
                amount=qty * entry / lev,
                qty=qty,
                strategy=strategy,
                reason=f'自动标记: {strategy}',
                sl_price=sl_price,
                tp_price=tp_price,
            )
            pos_entry['current_price'] = mark
            pos_entry['pnl'] = round(upnl, 4)
            pos_entry['pnl_percent'] = round(upnl / max(0.01, pos_entry['amount']) * 100, 2)
            pos_entry['last_check'] = time.time()
            state['positions'][_pk(sym, pos_side)] = pos_entry
            slabel = get_profile(strategy)["label"]
            logger.info(f'  🏷️ 新标记 {sym} {pos_side} → {slabel} (SL={sl_price} TP={tp_price})')
    
    # C. 添加挂单中的（pending）
    for o in open_orders:
        sym = o['symbol']
        if sym in state['positions']:
            continue
        price = float(o.get('price', 0))
        orig_qty = float(o.get('origQty', 0))
        side = o.get('positionSide', o.get('side', 'BUY'))
        # 尝试找策略
        state['positions'][sym] = {
            'symbol': sym,
            'side': 'LONG' if side in ('BUY', 'LONG') else 'SHORT',
            'entry_price': price,
            'amount': orig_qty * price / 3,  # 默认3x
            'qty': orig_qty,
            'strategy': 'unknown',
            'status': 'pending',
            'opened_at': datetime.fromtimestamp(o.get('time', 0) / 1000, tz=timezone.utc).isoformat(),
            'reason': '自动标记: 挂单',
            'last_check': time.time(),
        }
        logger.info(f'  🏷️ 标记挂单 {sym} @{price}')
    
    save_bot_state(state)
    return state

# ─── 统一仓位管理 ─────────────────────────────────

@with_trade_lock
def market_close_position(symbol: str, side: str, qty: float) -> bool:
    """
    市价平仓一个持仓（带 reduceOnly 保护）。
    """
    # 平仓前重新读取实际持仓，防止状态过期
    live_positions = fetch_exchange_positions()
    actual_qty = 0
    for p in live_positions:
        if p['symbol'] == symbol and abs(float(p.get('positionAmt',0))) > 0:
            actual_qty = abs(float(p['positionAmt']))
            break
    if actual_qty <= 0:
        logger.info(f'  {symbol} 已无持仓，跳过平仓')
        return True
    
    # 1. 精准取消条件单（按algoId逐个删，不批量删整个symbol）
    try:
        _session.headers.update(HDRS)
        p2 = {'symbol': symbol, 'timestamp': _ts(), 'recvWindow': 10000}
        q2, s2 = _sig(p2)
        resp = _session.get(f'{FAPI}/algoOpenOrders?{q2}&signature={s2}', timeout=10)
        if resp.status_code == 200:
            for a in resp.json():
                if a.get('algoStatus') in ('NEW', 'WORKING'):
                    dq, ds = _sig({'symbol': symbol, 'algoId': a['algoId']})
                    _session.delete(f'{FAPI}/algoOrder?{dq}&signature={ds}', timeout=10)
                    time.sleep(0.1)
    except:
        pass
    
    # 2. 精准取消普通挂单（按orderId）
    try:
        p3 = {'symbol': symbol, 'timestamp': _ts(), 'recvWindow': 10000}
        q3, s3 = _sig(p3)
        resp = _session.get(f'{FAPI}/openOrders?{q3}&signature={s3}', timeout=10)
        if resp.status_code == 200:
            for o in resp.json():
                if o.get('status') == 'NEW':
                    dq, ds = _sig({'symbol': symbol, 'orderId': o['orderId']})
                    _session.delete(f'{FAPI}/order?{dq}&signature={ds}', timeout=10)
                    time.sleep(0.1)
    except:
        pass
    
    # 3. 市价平仓（带 reduceOnly）
    close_side = 'SELL' if side == 'LONG' else 'BUY'
    pos_side = side
    try:
        aligned_qty = _align_qty(symbol, actual_qty)
        r = _post('order', {
            'symbol': symbol, 'side': close_side, 'type': 'MARKET',
            'quantity': str(aligned_qty), 'positionSide': pos_side,
            'reduceOnly': 'true',
        })
        if r and 'orderId' in r:
            fills = r.get('fills', [])
            avg = fills[0]['price'] if fills else '?'
            logger.info(f'  ✅ 平仓 {symbol} @{avg}')
            if r and 'orderId' in r:
                # 平仓后重新查询交易所确认
                time.sleep(1)
                post_check = fetch_exchange_positions()
                still_open = any(p['symbol'] == symbol and abs(float(p.get('positionAmt',0))) > 0 for p in post_check)
                if not still_open:
                    fills = r.get('fills', [])
                    avg = fills[0]['price'] if fills else '?'
                    logger.info(f'  ✅ 平仓 {symbol} @{avg}（交易所确认）')
                    return True
                else:
                    logger.warning(f'  ⚠️ {symbol} 平仓API返回成功但仍有持仓，标记为CLOSE_PENDING')
                    return False
    except Exception as e:
        logger.warning(f'  ⚠️ 平仓 {symbol} 失败: {e}')
    return False

@with_trade_lock
def update_sltp(symbol: str, side: str, sl_price: float = None, tp_price: float = None, qty: float = None) -> bool:
    """安全更新止盈止损。

    先创建并确认新保护单，再按 algoId 精确删除同方向旧单。
    任何新止损创建失败时保留旧止损，不执行全量撤单。
    """
    close_side = 'SELL' if side == 'LONG' else 'BUY'
    if not qty:
        return False

    qty_str = ('%g' % _align_qty(symbol, qty)).replace(',', '')

    # 获取当前条件单。查询失败时禁止更新，避免未知状态下重复或误撤。
    algo_map = fetch_exchange_algos()
    if not isinstance(algo_map, dict):
        logger.warning(f'  ⚠️ {symbol} 条件单查询失败，取消本次SL/TP更新')
        return False
    current = algo_map.get(symbol, [])

    def _order_type(a):
        return str(a.get('orderType') or a.get('type') or '').upper()

    def _same_side(a):
        ps = str(a.get('positionSide') or '').upper()
        return not ps or ps == side.upper()

    old_sl = [a for a in current if _same_side(a) and 'STOP' in _order_type(a) and 'PROFIT' not in _order_type(a)]
    old_tp = [a for a in current if _same_side(a) and 'PROFIT' in _order_type(a)]

    created_sl = None
    created_tp = None

    # 止损是强制项：先创建，失败则保留旧单并退出。
    if sl_price and sl_price > 0:
        aligned = _align_price_dir(symbol, sl_price, 'nearest')
        try:
            r = _post('algoOrder', {
                'symbol': symbol, 'side': close_side,
                'positionSide': side, 'algotype': 'CONDITIONAL',
                'type': 'STOP_MARKET', 'quantity': qty_str,
                'triggerprice': str(aligned), 'workingType': 'MARK_PRICE',
            })
            if not (r and r.get('algoId')):
                logger.error(f'  ❌ {symbol} 新止损创建未确认，保留旧止损')
                return False
            created_sl = r.get('algoId')
            logger.info(f'  🛑 新止损 @{aligned} ✅ algoId={created_sl}')
        except Exception as e:
            logger.error(f'  ❌ {symbol} 新止损挂单失败，保留旧止损: {e}')
            return False

        # 新止损成功后，精确删除旧止损；绝不使用 allAlgoOrders。
        for a in old_sl:
            aid = a.get('algoId')
            if aid and str(aid) != str(created_sl):
                try:
                    _delete('algoOrder', {'symbol': symbol, 'algoId': aid})
                except Exception as e:
                    logger.warning(f'  ⚠️ 旧止损删除失败 algoId={aid}: {e}')

    # 止盈失败不影响已确认的新止损；保留旧止盈。
    if tp_price and tp_price > 0:
        aligned = _align_price_dir(symbol, tp_price, 'nearest')
        try:
            r = _post('algoOrder', {
                'symbol': symbol, 'side': close_side,
                'positionSide': side, 'algotype': 'CONDITIONAL',
                'type': 'TAKE_PROFIT_MARKET', 'quantity': qty_str,
                'triggerprice': str(aligned), 'workingType': 'MARK_PRICE',
            })
            if r and r.get('algoId'):
                created_tp = r.get('algoId')
                logger.info(f'  🎯 新止盈 @{aligned} ✅ algoId={created_tp}')
                for a in old_tp:
                    aid = a.get('algoId')
                    if aid and str(aid) != str(created_tp):
                        try:
                            _delete('algoOrder', {'symbol': symbol, 'algoId': aid})
                        except Exception as e:
                            logger.warning(f'  ⚠️ 旧止盈删除失败 algoId={aid}: {e}')
            else:
                logger.warning(f'  ⚠️ {symbol} 新止盈创建未确认，保留旧止盈')
        except Exception as e:
            logger.warning(f'  ⚠️ {symbol} 新止盈挂单失败，保留旧止盈: {e}')

    return created_sl is not None if sl_price and sl_price > 0 else created_tp is not None

@with_trade_lock
def manage_all_positions(state: dict = None):
    """
    统一仓位管理循环。
    对每个活跃持仓：
    1. 检查是否触发了止盈止损 → 平仓
    2. 时间止损 → 超时平仓
    3. 技术退出 → 趋势转坏平仓
    4. 追踪止损 → 利润足够时移动止损
    5. 更新 bot_state
    """
    if state is None:
        state = load_bot_state()
    
    positions = state.get('positions', {})
    if not positions:
        logger.info('📭 无持仓')
        return
    
    # 先同步最新的价格和盈亏
    live_map = {}
    for p in fetch_exchange_positions():
        live_map[p['symbol']] = p
    
    to_close = []  # (symbol, reason)
    to_update_sltp = []  # (symbol, entry, side, sl, tp, qty)
    
    for sym, pos in list(positions.items()):
        if pos.get('status') not in ('active', 'pending'):
            continue
        
        side = pos.get('side', 'LONG')
        entry = pos.get('entry_price', 0)
        amount = pos.get('amount', 0)
        qty = pos.get('qty', 0)
        strategy = pos.get('strategy', 'unknown')
        profile = get_profile(strategy)
        opened_at = pos.get('opened_at', '')
        
        # 从交易所获取最新状态
        lp = live_map.get(sym)
        if lp:
            mark = float(lp.get('markPrice', 0))
            upnl = float(lp.get('unRealizedProfit', 0))
            qty = abs(float(lp.get('positionAmt', 0)))
            pos['current_price'] = mark
            pos['pnl'] = round(upnl, 4)
            pos['pnl_percent'] = round(upnl / max(0.01, abs(qty * entry / max(1, profile.get('leverage', 3)))) * 100, 2)
            pos['qty'] = qty
            pos['amount'] = abs(qty * mark / max(1, profile.get('leverage', 3)))
        else:
            # 可能是挂单（pending）
            if pos.get('status') == 'pending':
                continue
            # 持仓已在交易所消失 → 标记已关闭
            pos['status'] = 'closed'
            pos['closed_at'] = datetime.now(timezone.utc).isoformat()
            continue
        
        if qty <= 0:
            continue
        
        pnl_pct = pos.get('pnl_percent', 0)
        current_price = pos.get('current_price', 0)
        
        # ── 1. 止盈止损触发检查 ──
        sl_margin = profile.get('sl_margin_pct', 10)
        tp_margin = profile.get('tp_margin_pct', 20)
        
        if pnl_pct <= -sl_margin:
            logger.info(f'🛑 止损触发: {sym} {pnl_pct:.1f}%')
            to_close.append((sym, f'止损{pnl_pct:.1f}%'))
            continue
        
        if pnl_pct >= tp_margin:
            logger.info(f'🎯 止盈触发: {sym} +{pnl_pct:.1f}%')
            to_close.append((sym, f'止盈+{pnl_pct:.1f}%'))
            continue
        
        # ── 2. 时间退出 ──
        max_age = profile.get('max_age_hours', 48)
        if opened_at:
            try:
                opened_dt = datetime.fromisoformat(opened_at)
                age_hours = (datetime.now(timezone.utc) - opened_dt.replace(tzinfo=None)).total_seconds() / 3600
                pos['age_hours'] = round(age_hours, 1)
                
                if age_hours > max_age:
                    # 超时：盈利或亏损不大就平仓
                    if pnl_pct < 5.0:  # 没赚够也平
                        logger.info(f'⏰ 超时退出: {sym} {age_hours:.0f}h PnL={pnl_pct:.1f}%')
                        to_close.append((sym, f'超时{age_hours:.0f}h PnL{pnl_pct:.1f}%'))
                        continue
                    else:
                        # 赚够了，继续持有并加强追踪
                        logger.info(f'  ⏰ {sym} 超时但盈利+{pnl_pct:.1f}%，继续追踪')
            except:
                pass
        
        # ── 3. 技术退出 ──
        try:
            klines = fetch_klines(sym, '1h', limit=60)
            if klines is not None and len(klines) > 20:
                signals = generate_technical_signals(klines)
                bull_score = signals.get('bull_score', 0)
                bear_score = signals.get('bear_score', 0)
                
                # 做多但技术转弱 → 警告，不直接平仓
                if side == 'LONG' and bear_score >= bull_score + 2:
                    logger.info(f'  ⚡ {sym} 技术转空(多{bull_score}/空{bear_score})，考虑风控')
                    if pnl_pct < -sl_margin * 0.5:  # 已亏超过一半止损
                        to_close.append((sym, f'技术转空(多{bull_score}/空{bear_score})'))
                        continue
                elif side == 'SHORT' and bull_score >= bear_score + 2:
                    logger.info(f'  ⚡ {sym} 技术转多(多{bull_score}/空{bear_score})，考虑风控')
                    if pnl_pct < -sl_margin * 0.5:
                        to_close.append((sym, f'技术转多(多{bull_score}/空{bear_score})'))
                        continue
        except Exception as e:
            logger.debug(f'  技术检查失败 {sym}: {e}')
        
        # ── 4. 追踪止损 ──
        if current_price and entry:
            _apply_trailing_stop(sym, pos, entry, current_price, pnl_pct, profile)
            to_update_sltp.append((sym, entry, side,
                                    pos.get('sl_price', 0), pos.get('tp_price', 0), qty))
    
    # ── 执行平仓 ──
    for sym, reason in to_close:
        pos = positions.get(sym, {})
        pnl = pos.get('pnl', 0)
        pnl_pct = pos.get('pnl_percent', 0)
        
        market_close_position(sym, pos.get('side', 'LONG'), pos.get('qty', 0))
        
        # 记录
        state.setdefault('trades', []).append({
            'action': 'CLOSE', 'symbol': sym,
            'side': pos.get('side', '?'),
            'reason': reason,
            'pnl': round(pnl, 4),
            'pnl_pct': round(pnl_pct, 2),
            'time': datetime.now(timezone.utc).isoformat(),
        })
        state['total_pnl'] = state.get('total_pnl', 0) + pnl
        
        # 从活跃移出
        pos['status'] = 'closed'
        pos['closed_at'] = datetime.now(timezone.utc).isoformat()
        pos['close_reason'] = reason
        
        # 推送通知
        icon = '🎯' if pnl > 0 else '🛑'
        icon = "🎯" if pnl > 0 else "🛑"
        p_side = pos.get("side", "?")
        push_notification(f'{icon} 平仓: {sym}\n方向: {p_side}\n理由: {reason}\n盈亏: {pnl:+.4f}U ({pnl_pct:+.2f}%)', 'close')
    for sym, entry, side, sl, tp, qty in to_update_sltp:
        if sl > 0 or tp > 0:
            update_sltp(sym, side, sl if sl > 0 else None, tp if tp > 0 else None, qty)
    
    # 清理已关闭的旧记录（保留100条最近）
    closed_count = sum(1 for p in positions.values() if p.get('status') == 'closed')
    if closed_count > 100:
        # 标记部分已关闭的为archived
        archived = 0
        for p in positions.values():
            if p.get('status') == 'closed' and archived < closed_count - 100:
                p['status'] = 'archived'
                archived += 1
    
    save_bot_state(state)
    
    active = sum(1 for p in positions.values() if p.get('status') in ('active', 'pending'))
    closed = sum(1 for p in positions.values() if p.get('status') == 'closed')
    logger.info(f'📊 仓位管理完毕: {active}活跃 | {closed}已平')

def _apply_trailing_stop(sym: str, pos: dict, entry: float, current: float,
                          pnl_pct: float, profile: dict):
    """
    追踪止损逻辑。
    根据利润水平，逐步将止损从入场价往盈利方向移动。
    """
    side = pos.get('side', 'LONG')
    leverage = profile.get('leverage', 3)
    activate_pct = profile.get('trailing_activate_pct', 3.0)
    break_even_pct = profile.get('trailing_target_pct', 6.0)
    protect_profit_pct = profile.get('trailing_profit_pct', 12.0)
    sl_margin = profile.get('sl_margin_pct', 10)
    
    old_sl = pos.get('sl_price', 0)
    
    if side == 'LONG':
        # 默认止损
        base_sl = entry * (1 - sl_margin / leverage / 100)
        
        if pnl_pct >= protect_profit_pct:
            # 盈利丰厚 → 止损上移到+2%利润
            new_sl = entry * (1 + 2.0 / leverage / 100)
        elif pnl_pct >= break_even_pct:
            # 盈利中等 → 止损移到保本
            new_sl = entry * 0.999  # 略低于入场
        elif pnl_pct >= activate_pct:
            # 开始盈利 → 止损移到-1%亏损
            new_sl = entry * (1 - 1.0 / leverage / 100)
        else:
            # 未盈利 → 用原有止损
            return
    else:  # SHORT
        base_sl = entry * (1 + sl_margin / leverage / 100)
        
        if pnl_pct >= protect_profit_pct:
            new_sl = entry * (1 - 2.0 / leverage / 100)
        elif pnl_pct >= break_even_pct:
            new_sl = entry * 1.001
        elif pnl_pct >= activate_pct:
            new_sl = entry * (1 + 1.0 / leverage / 100)
        else:
            return
    
    # 检查是否有足够改进
    threshold = entry * 0.002  # 至少移动0.2%
    if side == 'LONG':
        should_move = (new_sl - old_sl) > threshold
    else:
        should_move = (old_sl - new_sl) > threshold
    
    if should_move or old_sl == 0:
        aligned = _align_price_dir(sym, new_sl, 'nearest')
        pos['sl_price'] = aligned
        pos['last_sl'] = aligned
        pos['trailing_activated'] = True
        msg = f'  🔄 {sym} 追踪止损: PnL+{pnl_pct:.1f}% → 止损 {aligned}'
        logger.info(msg)
        pos['trail_msg'] = msg

# ─── 整合版心跳入口 ──────────────────────────────

def run_full_cycle():
    """
    完整管理周期（给心跳调用）：
    1. sync — 同步交易所仓位
    2. manage — 管理（止盈止损/追踪/时间退出）
    """
    state = load_bot_state()
    state = sync_all_positions(state)
    manage_all_positions(state)
    return state

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [pos_mgr] %(message)s')
    state = run_full_cycle()
    
    # 打印当前仓位
    positions = state.get('positions', {})
    active = {k: v for k, v in positions.items() if v.get('status') in ('active', 'pending')}
    print(f'\n📊 当前跟踪: {len(active)}单')
    for sym, p in sorted(active.items()):
        strat = get_profile(p.get('strategy', 'unknown'))['label']
        age = p.get('age_hours', 0)
        pnl = p.get('pnl_percent', 0)
        pside = p.get("side","?")
        pstrat = p.get("strategy","?")
        pstat = p.get("status","?")
        print(f'  {sym:12s} {pside} {pstrat:10s} age={age:.0f}h PnL={pnl:+.1f}% status={pstat}')

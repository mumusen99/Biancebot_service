# ⚠️ FROZEN: legacy module. Only fatal security fixes allowed. No new features.
#!/usr/bin/env python3
"""
统一仓位管理器
==============
职责：追踪交易所所有持仓 → 自动打标 → 统一管理 → 动态止损

1. sync_all_positions() — 从交易所拉所有持仓，与 bot_state 同步，未标记的自动打标
2. manage_all_positions() — 统一管理：止盈止损触发、追踪止损、时间退出、技术退出
"""
import json, time, logging, urllib.parse, hmac, hashlib, math, os, functools
from pathlib import Path
from datetime import datetime, timezone
import requests as req
import pandas as pd

from trading_bot.core.settings import API_KEY, API_SECRET, PROXY, BOT_STATE_FILE, STRATEGY_PROFILES
from trading_bot.exchange.client import _align_price_dir, _align_qty, _get_symbol_precision
from trading_bot.exchange.market_data import fetch_klines, fetch_ticker
from trading_bot.strategy.indicators import generate_technical_signals
from trading_bot.integrations.notifications import push as push_notification
from trading_bot.portfolio.reconciler import PositionReconciler, ReconciliationReport
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
# ── 统一端点 — 通过 ExchangeGateway ──
from trading_bot.exchange.gateway import get_gateway
_gw = get_gateway()
FAPI = _gw._fapi_v1
FA2 = _gw._fapi_v2
_session = req.Session()
_session.proxies = {'http': PROXY, 'https': PROXY}
HDRS = {'X-MBX-APIKEY': API_KEY}

# ─── 策略止盈止损参数 ─────────────────────────────

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

# ── 底层 REST 方法委托给 ExchangeGateway ──

def _get(path: str, params: dict = None) -> dict:
    """通过 gateway 读取（签名版GET）。"""
    rid = _gw._request_id()
    try:
        return _gw._call("GET", FAPI, path, params or {}, rid, "")
    except Exception:
        return None

def _post(path: str, params: dict = None) -> dict:
    """通过 gateway 写入（签名版POST）。"""
    rid = _gw._request_id()
    try:
        return _gw._call("POST", FAPI, path, params or {}, rid, "")
    except Exception:
        return None

def _delete(path: str, params: dict = None) -> dict:
    """通过 gateway 删除（签名版DELETE）。"""
    rid = _gw._request_id()
    try:
        return _gw._call("DELETE", FAPI, path, params or {}, rid, "")
    except Exception:
        return None

def _get_price(symbol: str) -> float:
    """获取最新价"""
    r = _get('ticker/price', {'symbol': symbol})
    return float(r['price'])

def load_bot_state() -> dict:
    from trading_bot.storage.state_store import load_state_safe
    return load_state_safe(BOT_STATE_FILE)

def save_bot_state(state: dict):
    from trading_bot.storage.state_store import save_state_atomic
    save_state_atomic(BOT_STATE_FILE, state)


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


from trading_bot.execution.idempotency import get_idempotency_store

def check_signal_idempotent(signal_id: str) -> bool:
    """检查信号是否已执行过（持久化防重复开仓），返回 True=可执行"""
    store = get_idempotency_store()
    if store.is_duplicate(signal_id):
        return False
    store.mark_pending(signal_id, "entry")
    return True

def with_trade_lock(func):
    """交易互斥锁装饰器。锁失败时记录警告并跳过。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not acquire_trade_lock():
            logger.warning(f'⏳ 交易锁被占用，跳过 {func.__name__}')
            return False if func.__name__.startswith('market_close') else None
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
    try:
        positions = _gw.get_positions()
        return [{'symbol': p.symbol, 'positionSide': p.position_side.value,
                 'positionAmt': str(p.position_amt), 'entryPrice': str(p.entry_price),
                 'markPrice': str(p.mark_price), 'unRealizedProfit': str(p.unrealized_pnl),
                 'leverage': str(p.leverage), 'notional': str(p.notional)}
                for p in positions]
    except Exception as e:
        logger.warning(f'  ⚠️ positionRisk 失败: {e}')
        return []

def fetch_exchange_orders(symbol: str = None) -> list:
    """获取普通挂单"""
    try:
        orders = _gw.get_open_orders(symbol)
        return [{'symbol': o.symbol, 'orderId': o.order_id, 'positionSide': o.position_side.value,
                 'type': o.order_type.value, 'status': o.status.value}
                for o in orders]
    except Exception:
        return []

def fetch_exchange_algos() -> dict:
    """获取条件委托（止盈止损），按 symbol 分组"""
    try:
        algos = _gw.get_algo_orders()
        result = {}
        for a in algos:
            result.setdefault(a.symbol, []).append({
                'symbol': a.symbol, 'algoId': str(a.order_id),
                'positionSide': a.position_side.value,
                'type': a.order_type.value, 'stopPrice': str(a.stop_price),
            })
        return result
    except Exception:
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
    
    # ── 使用 PositionReconciler 统一协调 ──
    reconciler = PositionReconciler()
    report = reconciler.reconcile(state['positions'])
    logger.info(f'📡 交易所: {report.exchange_positions}持仓, {report.exchange_orders}挂单 | {report.summary}')
    
    # ── 应用安全修复 ──
    if report.requires_halt:
        logger.error("HALT: %s — refusing to modify state", report.halt_reason)
        return state
    
    # A. 清除已平仓的本地记录
    for item in report.stale_local:
        key = item['local_key']
        if key in state['positions']:
            logger.info(f'  🗑️ {key} 已平仓，移除')
            del state['positions'][key]
    
    # A2. 恢复交易所有但本地closed的持仓
    for key in report.reactivated:
        if key in state['positions']:
            state['positions'][key]['status'] = 'active'
            # 如果没有sl_price，根据entry补一个
            entry = float(state['positions'][key].get('entry_price', 0))
            if entry > 0 and not state['positions'][key].get('sl_price'):
                side = state['positions'][key].get('side', 'LONG')
                if side == 'LONG':
                    state['positions'][key]['sl_price'] = round(entry * 0.995, 8)
                else:
                    state['positions'][key]['sl_price'] = round(entry * 1.005, 8)
                state['positions'][key]['tp_price'] = round(entry * 1.015, 8)
            logger.info(f'  🔄 {key} 恢复为 active')
    
    # B. 拉取 algo orders 用于新持仓打标
    algos_raw = fetch_exchange_algos()
    
    # C. 处理交易所有但本地没有的（新持仓/外部开仓）
    for item in report.missing_local:
        sym = item['symbol']
        side = item['side']
        qty = item['qty']
        entry = item['entry_price']
        mark = item.get('mark_price', 0)
        local_key = item['local_key']
        
        # 自动打标
        sl_price = 0
        tp_price = 0
        if sym in algos_raw:
            for a in algos_raw[sym]:
                if a.get('algoStatus') != 'NEW':
                    continue
                if a.get('type') == 'STOP_MARKET':
                    sl_price = float(a.get('triggerPrice', 0))
                elif a.get('type') == 'TAKE_PROFIT_MARKET':
                    tp_price = float(a.get('triggerPrice', 0))
        
        lev = item.get('leverage', 3)
        strategy = detect_strategy(entry, sl_price, tp_price, lev)
        slabel = get_profile(strategy)["label"]
        
        pos_entry = create_position(
            symbol=sym, side=side, entry_price=entry,
            amount=qty * entry / lev, qty=qty,
            strategy=strategy, reason=f'自动标记: {slabel}',
            sl_price=sl_price, tp_price=tp_price,
        )
        pos_entry['current_price'] = mark
        pos_entry['pnl'] = round(item.get('pnl', 0), 4)
        pos_entry['last_check'] = time.time()
        state['positions'][local_key] = pos_entry
        logger.info(f'  🏷️ {sym} {side} → {slabel} (SL={sl_price} TP={tp_price})')
    
    # D. 处理数量不匹配 — 仅更新本地qty，不修改保护单
    for item in report.quantity_mismatches:
        local_key = item['local_key']
        if local_key in state['positions']:
            state['positions'][local_key]['qty'] = item['exchange_qty']
            logger.info(f'  🔄 {local_key} qty: {item["local_qty"]}→{item["exchange_qty"]}')
    
    # E. 处理缺失止损（记录告警，实际挂单由 scalper 处理）
    if report.missing_stop_orders:
        for item in report.missing_stop_orders:
            logger.warning(f'  ⚠️ {item["symbol"]} {item["side"]} 无止损保护! qty={item["qty"]}')
    
    # F. 更新已有持仓的实时价格/PnL
    for p in fetch_exchange_positions():
        sym = p['symbol']
        pos_side = p.get('positionSide', 'LONG')
        local_key = _pk(sym, pos_side)
        if local_key in state['positions']:
            pos = state['positions'][local_key]
            pos['current_price'] = float(p.get('markPrice', 0))
            pnl = round(float(p.get('unRealizedProfit', 0)), 4)
            pos['pnl'] = pnl
            pos['last_check'] = time.time()
            # MFE/MAE 追踪（用于策略评估）
            if 'mfe' not in pos or pnl > pos['mfe']:
                pos['mfe'] = pnl
            if 'mae' not in pos or pnl < pos['mae']:
                pos['mae'] = pnl
    
    save_bot_state(state)
    return state

# ─── 统一仓位管理 ─────────────────────────────────

@with_trade_lock
def market_close_position(symbol: str, side: str, qty: float) -> bool:
    """
    市价平仓一个持仓。按 symbol+side 精确定位，不影响同币种反向仓。
    """
    # 平仓前重新读取实际持仓，防止状态过期
    live_positions = fetch_exchange_positions()
    actual_qty = 0
    for p in live_positions:
        p_side = str(p.get('positionSide', 'LONG')).upper()
        if p['symbol'] == symbol and p_side == side.upper() and abs(float(p.get('positionAmt',0))) > 0:
            actual_qty = abs(float(p['positionAmt']))
            break
    if actual_qty <= 0:
        logger.info(f'  {symbol}:{side} 已无持仓，跳过平仓')
        return False  # 不是我们平的，不算成功
    
    # 1. 精准取消条件单（仅当前方向）
    try:
        algos = _gw.get_algo_orders(symbol)
        for a in algos:
            a_side = str(getattr(a, 'position_side', '')).upper()
            if a_side != side.upper():
                continue
            if getattr(a, 'status', None) and a.status.value in ('NEW', 'SUBMITTED'):
                _gw.cancel_algo_order(symbol, str(a.order_id))
                time.sleep(0.1)
    except Exception:
        pass
    
    # 2. 精准取消普通挂单（仅当前方向）
    try:
        orders = _gw.get_open_orders(symbol)
        for o in orders:
            o_side = str(getattr(o, 'position_side', '')).upper()
            if o_side != side.upper():
                continue
            if getattr(o, 'status', None) and o.status.value == 'SUBMITTED':
                _gw.cancel_order(symbol, str(o.order_id))
                time.sleep(0.1)
    except Exception:
        pass
    
    # 3. 市价平仓
    close_side = 'SELL' if side == 'LONG' else 'BUY'
    pos_side = side
    try:
        aligned_qty = _align_qty(symbol, actual_qty)
        r = _post('order', {
            'symbol': symbol, 'side': close_side, 'type': 'MARKET',
            'quantity': str(aligned_qty), 'positionSide': pos_side,
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
def market_close_partial(symbol: str, side: str, fraction: float) -> tuple:
    """平仓指定比例的仓位，返回 (成功, 剩余数量, 成交均价)。
    
    fraction: 0.0-1.0，要平掉的比例（基于交易所实际持仓）
    """
    live_positions = fetch_exchange_positions()
    actual_qty = 0.0
    for p in live_positions:
        if p['symbol'] == symbol:
            actual_qty = abs(float(p.get('positionAmt', 0)))
            break
    if actual_qty <= 0:
        logger.info(f'  {symbol} 已无持仓，跳过部分平仓')
        return (True, 0, 0)
    
    close_qty = actual_qty * fraction
    if close_qty < 0.001:
        logger.info(f'  {symbol} 平仓量{close_qty:.6f}过小，全平')
        return (market_close_position(symbol, side, actual_qty), 0, 0)
    
    close_side = 'SELL' if side == 'LONG' else 'BUY'
    try:
        aligned_qty = _align_qty(symbol, close_qty)
        r = _post('order', {
            'symbol': symbol, 'side': close_side, 'type': 'MARKET',
            'quantity': str(aligned_qty), 'positionSide': side,
        })
        if r and 'orderId' in r:
            fills = r.get('fills', [])
            avg = fills[0]['price'] if fills else 0
            remaining = actual_qty - close_qty
            logger.info(f'  ✂️ 部分平仓 {symbol} {fraction*100:.0f}% qty={aligned_qty} @{avg} 剩余≈{remaining:.1f}')
            return (True, remaining, float(avg))
    except Exception as e:
        logger.warning(f'  ⚠️ 部分平仓 {symbol} 失败: {e}')
    return (False, actual_qty, 0)

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

def manage_all_positions(state: dict = None):
    """仓位管理：仅同步交易所价格和标记已平仓。止盈止损由 engine.py 1s检查处理。"""
    if state is None:
        state = load_bot_state()
    
    positions = state.get('positions', {})
    if not positions:
        return
    
    # 同步交易所最新状态
    live_map = {}
    for p in fetch_exchange_positions():
        live_map[p['symbol']] = p
    
    active_count = 0
    closed_count = 0
    
    for sym, pos in list(positions.items()):
        if pos.get('status') not in ('active', 'pending'):
            closed_count += 1
            continue
        
        exchange_sym = sym.split(':')[0] if ':' in sym else sym
        lp = live_map.get(exchange_sym)
        if not lp:
            if pos.get('status') == 'pending':
                continue
            pos['status'] = 'closed'
            pos['closed_at'] = datetime.now(timezone.utc).isoformat()
            closed_count += 1
            continue
        
        qty = abs(float(lp.get('positionAmt', 0)))
        if qty <= 0:
            pos['status'] = 'closed'
            pos['closed_at'] = datetime.now(timezone.utc).isoformat()
            closed_count += 1
            continue
        
        # 更新价格和PnL
        entry = pos.get('entry_price', 0)
        mark = float(lp.get('markPrice', 0))
        upnl = float(lp.get('unRealizedProfit', 0))
        pos['current_price'] = mark
        pos['pnl'] = round(upnl, 4)
        margin = abs(qty * entry / 5)
        pos['pnl_percent'] = round(upnl / max(0.01, margin) * 100, 2)
        pos['qty'] = qty
        pos['amount'] = abs(qty * entry / 5)
        active_count += 1
    
    logger.info(f'📊 仓位管理完毕: {active_count}活跃 | {closed_count}已平')
    
    save_bot_state(state)

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
    save_bot_state(state)
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

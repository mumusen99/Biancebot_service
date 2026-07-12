# ⚠️ FROZEN: legacy module. Only fatal security fixes allowed. No new features.
"""
舔一口就跑 🏃‍♂️ 超短线反转策略
==============================
分层决策（2026-07-08 升级）:
第1层 BTC环境判断 → 决定多空方向
第2层 Relative Strength + 成交量 + 均线偏离 → 筛选候选
第3层 5m RSI/布林带 → 确认入场时机

MARKET 市价入场，5x杠杆，止盈+10%保证金/止损-5%保证金。
每次运行先检查已有 scalp 持仓的止盈止损是否精确卡在目标值。
"""
import json
import time
import logging
import hmac
import hashlib
import urllib.parse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests as req

from trading_bot.core.settings import (
    API_KEY, API_SECRET, PROXY, BOT_STATE_FILE,
)
from trading_bot.data.ws_market_client import market_cache
from trading_bot.exchange.market_data import fetch_ticker, fetch_klines
from trading_bot.exchange.client import _api, IS_TESTNET, LIVE_FAPI, TESTNET_FAPI, _align_price_dir
from trading_bot.strategy.market_regime import get_btc_environment, scan_top_coins
from trading_bot.integrations.notifications import notify_trading_status
from trading_bot.strategy.regime_detector import (
    detect_regime_probabilities, get_position_confidence_factor,
)
from trading_bot.core.models import position_key, split_position_key, migrate_position_keys
from trading_bot.storage.state_store import save_state_atomic, load_state_safe
from trading_bot.exchange.protection import (
    ensure_position_protection, repair_existing_protection,
    cancel_all_protection, _get_algo_orders, _cancel_algo,
    ensure_partial_tp_protection,
)

# 仅在独立运行时配置日志，避免导入时干扰调用者
if not logging.getLogger().hasHandlers():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [scalper] %(message)s')
logger = logging.getLogger('scalper')

BASE_DIR = Path(__file__).parent
# 统一端点（测试网/实盘）
from trading_bot.core.env_config import get_exchange_config
_FAPI_BASE = get_exchange_config().fapi_v1_base
_FAPI2_BASE = get_exchange_config().fapi_v2_base

# 开仓状态追踪（用于通知）
_last_can_open = True  # 假设初始允许
_last_stop_reasons = []

def _check_trading_status(can_open: bool, reasons: list, btc_env: dict = None):
    """检查开仓状态变化并通知。在 run_scalper 每次调用结束时调用。"""
    global _last_can_open, _last_stop_reasons
    if can_open == _last_can_open and reasons == _last_stop_reasons:
        return  # 无变化
    # 只在状态真正变化时通知
    if can_open != _last_can_open:
        _last_can_open = can_open
        _last_stop_reasons = reasons
        notify_trading_status(can_open, reasons, btc_env)

# 兼容旧代码的别名
FAPI_BASE = _FAPI_BASE
_session = req.Session()
_session.proxies = {'http': PROXY, 'https': PROXY}

# ─── 策略参数 ──────────────────────────────────────
SCALP_LEVERAGE = 3               # 3x 杠杆
SCALP_MARGIN = 10.0              # 每单保证金10U → 持仓30U (10×3)
SCALP_BUDGET = 60.0               # 总预算60U（最多6单×10U保证金）
SCALP_MAX_POSITIONS = 5           # 最多同时持有5个超短线单

# 全局总仓位上限（所有策略合计，防止多脚本叠加开单）
MAX_TOTAL_POSITIONS = 10          # 超过此数不再开新单

SL_PRICE_PCT = 1.8 / SCALP_LEVERAGE      # 0.6%价格 → -1.8%保证金（0.4%太窄被山寨噪音扫损）
TP_PRICE_PCT = 3.6 / SCALP_LEVERAGE     # 1.2%价格 → +3.6%保证金（盈亏比保持1:2）

# 信号阈值
TIMEFRAME = '5m'
KLINES_LIMIT = 60
BLOCKLIST = set()

# ─── 暴涨冷却追踪 ────────────────────────────
_PUMP_COOLDOWNS = {}  # {symbol: cooldown_until_timestamp}

# ─── 入场评分阈值（运行时热加载） ─────────────────
_ENTRY_CFG = {}

def _load_entry_config(cfg: dict):
    """加载运行时入场评分参数"""
    global _ENTRY_CFG
    s = cfg.get('strategy', {})
    e = s.get('entry', {})
    r = s.get('rsi', {})
    em = s.get('ema', {})
    v = s.get('volume', {})
    ep = s.get('early_pullback', {})
    ac = s.get('anti_chase', {})
    pb = s.get('pullback', {})
    st = s.get('stop', {})
    _ENTRY_CFG = {
        'skip': float(e.get('min_score_skip', 6.0)),
        'limit': float(e.get('min_score_limit', 6.0)),
        'aggressive': float(e.get('min_score_aggressive_limit', 7.5)),
        'market': float(e.get('min_score_market', 9.5)),
        'limit_ttl': int(e.get('limit_ttl_seconds', 90)),
        'agg_ttl': int(e.get('aggressive_limit_ttl_seconds', 45)),
        'rsi_l_ideal': (float(r.get('long_ideal_min',44)), float(r.get('long_ideal_max',54))),
        'rsi_l_ok': (float(r.get('long_allowed_min',40)), float(r.get('long_allowed_max',58))),
        'rsi_s_ideal': (float(r.get('short_ideal_min',46)), float(r.get('short_ideal_max',56))),
        'rsi_s_ok': (float(r.get('short_allowed_min',42)), float(r.get('short_allowed_max',60))),
        'ema_ideal': float(em.get('ideal_distance_pct', 0.25)),
        'ema_normal': float(em.get('normal_distance_pct', 0.50)),
        'ema_max': float(em.get('max_distance_pct', 0.70)),
        'ema_hard': float(em.get('hard_reject_distance_pct', 1.00)),
        'vol_reject': float(v.get('hard_reject_ratio', 0.35)),
        'vol_normal': float(v.get('normal_ratio', 0.50)),
        'vol_strong': float(v.get('strong_ratio', 0.80)),
        'early_enabled': bool(ep.get('enabled', True)),
        'early_risk': float(ep.get('risk_factor', 0.35)),
        # anti-chase
        'post_pump_cooldown_minutes': int(ac.get('post_pump_cooldown_minutes', 5)),
        'max_1m_move_atr': float(ac.get('max_1m_move_atr', 0.60)),
        'max_3m_move_atr': float(ac.get('max_3m_move_atr', 1.20)),
        'max_5m_move_atr': float(ac.get('max_5m_move_atr', 2.00)),
        'max_distance_vwap_atr': float(ac.get('max_distance_vwap_atr', 1.0)),
        'max_distance_ema20_atr': float(ac.get('max_distance_ema20_atr', 0.8)),
        # pullback quality
        'min_pullback_bars': int(pb.get('min_pullback_bars', 2)),
        'max_pullback_bars': int(pb.get('max_pullback_bars', 8)),
        'max_pullback_volume_ratio': float(pb.get('max_pullback_volume_ratio', 0.70)),
        # structural stop
        'atr_1m_buffer': float(st.get('atr_1m_buffer', 0.60)),
        'atr_5m_buffer': float(st.get('atr_5m_buffer', 0.25)),
        'minimum_buffer_pct': float(st.get('minimum_buffer_pct', 0.001)),
    }

# 舔头皮（Scalping）核心逻辑：
# 不追求抄底摸顶，而是顺着短期趋势方向，等回调到均线附近入场
# 关键原则：
#   1. 趋势过滤: 只在 EMA21 方向做单（价格在EMA上方→只做多，下方→只做空）
#   2. 回调入场: 等价格从 EMA21 拉回 EMA9 附近，确认支撑/阻力后再入场
#   3. 成交量确认: 回调量缩（空头力竭），启动量增（多头确认）
#   4. RSI中性区: 不在RSI<30或RSI>70时入场（那是趋势衰竭，不是回调）
#      理想RSI区间 LONG:40-55(从下方回归) SHORT:45-60(从上方回归)



def apply_runtime_config(cfg: dict) -> None:
    """Apply validated Hermes runtime parameters for future entries only."""
    global SCALP_LEVERAGE, SCALP_MARGIN, SCALP_BUDGET, SCALP_MAX_POSITIONS, MAX_TOTAL_POSITIONS
    risk = cfg.get("risk", {})
    SCALP_LEVERAGE = int(risk.get("leverage", SCALP_LEVERAGE))
    SCALP_MARGIN = float(risk.get("scalp_margin", SCALP_MARGIN))
    SCALP_BUDGET = float(risk.get("scalp_budget", SCALP_BUDGET))
    SCALP_MAX_POSITIONS = int(risk.get("scalp_max_positions", SCALP_MAX_POSITIONS))
    MAX_TOTAL_POSITIONS = int(risk.get("max_total_positions", MAX_TOTAL_POSITIONS))
    _load_entry_config(cfg)

# ═══════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════


def load_bot_state() -> dict:
    state = load_state_safe(BOT_STATE_FILE)
    return migrate_position_keys(state)


def save_bot_state(state: dict):
    save_state_atomic(BOT_STATE_FILE, state)


# ─── 垃圾单评分（越低越差，≤-3 可淘汰）───
def _junk_score(pos: dict, mark_price: float = 0) -> float:
    """返回 0 ~ -6，越低越垃圾。不扣减距 SL 距离（让 SL 自然触发）。"""
    score = 0.0
    now = time.time()
    entry = float(pos.get('entry_price', 0))
    if entry <= 0:
        return 0

    # 1m 动量（逆方向扣分）
    sym = pos.get('symbol', '')
    side = pos.get('side', 'LONG')
    try:
        df_1m = _fetch_klines_ws(sym, '1m', 3)
        if df_1m is not None and len(df_1m) >= 3:
            c = df_1m['close'].values
            mom = (c[-1] - c[0]) / c[0] * 100  # 最近 3 根 1m K 线变化%
            if (side == 'LONG' and mom < -0.15) or (side == 'SHORT' and mom > 0.15):
                score -= 2
            elif (side == 'LONG' and mom < 0) or (side == 'SHORT' and mom > 0):
                score -= 1
            elif (side == 'LONG' and mom > 0.05) or (side == 'SHORT' and mom < -0.05):
                score += 1  # 顺方向加分
    except:
        pass

    # 持仓时间过长
    opened = pos.get('opened_at') or pos.get('time', '')
    if opened:
        try:
            ot = datetime.fromisoformat(opened).timestamp()
            age_min = (now - ot) / 60
            if age_min > 45:
                score -= 2
            elif age_min > 30:
                score -= 1
        except:
            pass

    # 水下深度（浮亏%保证金）
    if mark_price > 0:
        qty = float(pos.get('qty') or 0)
        margin = qty * entry / 5  # 5x
        if margin > 0:
            if side == 'LONG':
                pnl = (mark_price - entry) * qty
            else:
                pnl = (entry - mark_price) * qty
            pnl_pct = pnl / margin * 100
            if pnl_pct < -5:
                score -= 2
            elif pnl_pct < -3:
                score -= 1
    
    return round(score, 1)


# ─── 连损减仓 + 日亏损熔断 ───
def _get_risk_state() -> dict:
    """从 bot_state 读取风险状态"""
    state = load_bot_state()
    return state.get('risk_state', {})

def _save_risk_state(rs: dict):
    state = load_bot_state()
    state['risk_state'] = rs
    save_bot_state(state)

def _reset_daily_if_needed(rs: dict):
    today = datetime.now().strftime('%Y%m%d')
    if rs.get('daily_date') != today:
        rs['daily_date'] = today
        rs['daily_pnl'] = 0.0

def record_trade_result(pnl: float, is_rotation: bool = False):
    """平仓时调用，更新连胜/连败和日盈亏。is_rotation=True 不计入连损统计"""
    rs = _get_risk_state()
    _reset_daily_if_needed(rs)
    rs['daily_pnl'] = rs.get('daily_pnl', 0) + pnl
    if not is_rotation:
        if pnl >= 0:
            rs['win_streak'] = rs.get('win_streak', 0) + 1
            rs['loss_streak'] = 0
        else:
            rs['loss_streak'] = rs.get('loss_streak', 0) + 1
            rs['win_streak'] = 0
    _save_risk_state(rs)

def get_position_multiplier() -> float:
    """返回仓位乘数：正常1.0，连输3单→0.5，连输5单→0.25，连胜2单恢复"""
    rs = _get_risk_state()
    loss = rs.get('loss_streak', 0)
    win = rs.get('win_streak', 0)
    if win >= 2:
        return 1.0  # 两连胜恢复
    if loss >= 5:
        return 0.25
    if loss >= 3:
        return 0.5
    return 1.0

def check_daily_limit() -> tuple[bool, str]:
    """日亏损5%熔断。返回(是否可开仓, 原因)"""
    rs = _get_risk_state()
    _reset_daily_if_needed(rs)
    daily_pnl = rs.get('daily_pnl', 0)
    # 从账户余额估算总资金
    try:
        from trading_bot.exchange.gateway import get_gateway
        bal = float(get_gateway().get_balance())
        if bal > 0:
            total = bal
        else:
            total = 237.0  # 默认
    except:
        total = 237.0
    if daily_pnl < 0 and abs(daily_pnl) > total * 0.05:
        return False, f'日亏损{abs(daily_pnl):.2f}U > 总资金{total:.1f}U的5%({total*0.05:.1f}U)，熔断'
    return True, ''


def _fetch_total_positions() -> int:
    """获取交易所当前所有持仓数（含所有策略）"""
    try:
        positions = _gw.get_positions()
        return len(positions)
    except Exception as e:
        raise Exception(f'positionRisk failed: {e}') from e


def _query_live_position(symbol: str, position_side: str) -> dict:
    """查询真实持仓；查询失败抛异常，不能把失败当空仓。"""
    try:
        positions = _gw.get_positions()
        for p in positions:
            if p.symbol == symbol and p.position_side.value.upper() == position_side.upper():
                return {'symbol': p.symbol, 'positionSide': p.position_side.value,
                        'positionAmt': str(p.position_amt), 'entryPrice': str(p.entry_price)}
        return {'symbol': symbol, 'positionSide': position_side, 'positionAmt': '0', 'entryPrice': '0'}
    except Exception as e:
        raise RuntimeError(f'positionRisk failed: {e}') from e


def _query_order(symbol: str, order_id: int) -> dict:
    return _gw.get_order(symbol, str(order_id)).raw_response if hasattr(
        _gw.get_order(symbol, str(order_id)), 'raw_response') else {}


def _cancel_entry_safely(symbol: str, order_id: int) -> bool:
    if not order_id:
        return True
    try:
        result = _gw.cancel_order(symbol, str(order_id))
        return result.success
    except Exception as e:
        # 已成交或已取消时查询确认
        try:
            status = str(_query_order(symbol, order_id).get('status', '')).upper()
            if status in ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'):
                return True
        except Exception:
            pass
        logger.error(f'取消入场单失败 {symbol} orderId={order_id}: {e}')
        return False


def _close_position_and_confirm(symbol: str, position_side: str, reason: str, retries: int = 5) -> bool:
    """按交易所真实数量紧急平仓并轮询确认归零。"""
    try:
        pos = _query_live_position(symbol, position_side)
        qty = abs(float(pos.get('positionAmt', 0) or 0))
        if qty <= 0:
            return True
        close_side = 'SELL' if position_side == 'LONG' else 'BUY'
        from trading_bot.exchange.client import _get_symbol_precision, _load_precisions
        _load_precisions()
        _, step, _, _ = _get_symbol_precision(symbol)
        qty_aligned = int(qty / step) * step
        qty_str = ('%g' % qty_aligned).replace(',', '')
        if float(qty_str) <= 0:
            logger.critical(f'{symbol} 紧急平仓数量低于精度，需人工处理 qty={qty}')
            return False
        _api('POST', 'order', {
            'symbol': symbol, 'side': close_side, 'type': 'MARKET',
            'quantity': qty_str, 'positionSide': position_side,
        })
        for _ in range(retries):
            time.sleep(0.8)
            check = _query_live_position(symbol, position_side)
            if abs(float(check.get('positionAmt', 0) or 0)) <= 0:
                logger.critical(f'🚨 紧急平仓已由交易所确认: {symbol} {position_side} | {reason}')
                return True
        logger.critical(f'🚨 紧急平仓请求后仓位仍存在: {symbol} {position_side} | {reason}')
        return False
    except Exception as e:
        logger.critical(f'🚨 紧急平仓确认失败: {symbol} {position_side}: {e}')
        return False


def _protect_filled_position(symbol: str, side: str, entry_price: float, qty: float,
                             risk_pct: float, reward_pct: float) -> tuple:
    """按真实成交均价和真实持仓数量建立保护，返回(sl,tp)。"""
    if side == 'LONG':
        raw_sl = entry_price * (1 - risk_pct / 100)
        raw_tp = entry_price * (1 + reward_pct / 100)
    else:
        raw_sl = entry_price * (1 + risk_pct / 100)
        raw_tp = entry_price * (1 - reward_pct / 100)
    from trading_bot.exchange.client import _align_sltp, _get_symbol_precision, _load_precisions
    sl_price, tp_price = _align_sltp(symbol, raw_sl, raw_tp, side)
    _load_precisions()
    _, step, _, _ = _get_symbol_precision(symbol)
    qty_aligned = int(qty / step) * step
    qty_str = ('%g' % qty_aligned).replace(',', '')
    if float(qty_str) <= 0:
        raise RuntimeError(f'真实持仓数量低于交易精度: {qty}')
    close_side = 'SELL' if side == 'LONG' else 'BUY'
    sl_order = _place_algo_order(symbol, close_side, side, 'STOP_MARKET', qty_str, sl_price)
    if not sl_order.get('algoId'):
        raise RuntimeError('止损订单未返回 algoId')
    try:
        _place_algo_order(symbol, close_side, side, 'TAKE_PROFIT_MARKET', qty_str, tp_price)
    except Exception as e:
        logger.warning(f'止盈创建失败，但止损已确认: {symbol}: {e}')
    return sl_price, tp_price

def _fetch_account_equity() -> float:
    """查询账户总权益（USDT），失败返回保守估计值。"""
    try:
        data = _gw._call("GET", _gw._fapi_v2, "account", {}, _gw._request_id(), "")
        return float(data.get('totalMarginBalance', 0) or 0)
    except Exception:
        return 200.0  # 保守估计


def _calc_sltp(entry: float, side: str, symbol: str = ''):
    """计算超短线止盈止损价格（动态对齐tick size）"""
    if side == 'LONG':
        sl = entry * (1 - SL_PRICE_PCT / 100)
        tp = entry * (1 + TP_PRICE_PCT / 100)
    else:
        sl = entry * (1 + SL_PRICE_PCT / 100)
        tp = entry * (1 - TP_PRICE_PCT / 100)
    if symbol:
        from trading_bot.exchange.client import _align_sltp
        sl, tp = _align_sltp(symbol, sl, tp, side)
    return round(sl, 8), round(tp, 8)


# ─── 通过 ExchangeGateway 统一出口 ───
from trading_bot.exchange.gateway import get_gateway
_gw = get_gateway()

def _place_algo_order(symbol: str, side: str, pos_side: str,
                       ord_type: str, qty: str, trigger: float):
    """挂条件单 — 委托给 ExchangeGateway。"""
    rid = _gw._request_id()
    from trading_bot.exchange.order_mapper import PositionSide, OrderRole, WorkingType
    ps = PositionSide.LONG if pos_side == "LONG" else PositionSide.SHORT
    role = OrderRole.STOP_LOSS if ord_type == "STOP_MARKET" else (
        OrderRole.TAKE_PROFIT if ord_type == "TAKE_PROFIT_MARKET" else OrderRole.TRAILING_STOP)
    params = {
        'symbol': symbol, 'side': side, 'positionSide': pos_side,
        'type': ord_type, 'quantity': qty,
        'stopPrice': str(_align_price_dir(symbol, trigger, 'nearest')),
        'workingType': 'MARK_PRICE', 'reduceOnly': 'true',
        'newClientOrderId': _gw._request_id()}
    return _gw._call("POST", _gw._fapi_v1, "algoOrder", params, rid, symbol)


def _cancel_algo(symbol: str, algo_id: int):
    """删除条件单 — 委托给 ExchangeGateway。"""
    try:
        _gw.cancel_algo_order(symbol, str(algo_id))
        return True
    except Exception:
        return False


def _cancel_limit_order(symbol: str, order_id: int):
    """取消普通挂单 — 委托给 ExchangeGateway。"""
    try:
        _gw.cancel_order(symbol, str(order_id))
        return True
    except Exception:
        return False


def _get_algo_orders(symbol: str) -> list:
    """查询条件委托 — 委托给 ExchangeGateway。"""
    try:
        rid = _gw._request_id()
        data = _gw._call("GET", _gw._fapi_v1, "allAlgoOrders",
                        {'symbol': symbol}, rid, symbol)
        return data if isinstance(data, list) else []
    except Exception:
        return []
from trading_bot.strategy.indicators import compute_scalp_indicators as compute_indicators
from trading_bot.strategy.position_review import (
    _fix_existing_sltp, _review_scalp_positions,
    _sync_pending_orders, _cleanup_stale_algos,
    _sync_orphan_positions,
)

def _fetch_klines_ws(symbol: str, timeframe: str = '5m', limit: int = 60):
    """WS缓存优先获取K线，失败回退REST"""
    # 尝试WS缓存（仅支持1m/5m）
    if timeframe in ('1m', '5m'):
        try:
            df = market_cache.get_klines_df(symbol, timeframe, limit)
            if df is not None and len(df) >= 20:
                return df
        except Exception:
            pass
    # 回退REST
    return fetch_klines(None, symbol, timeframe=timeframe, limit=limit)


def scan_signals() -> tuple:
    """
    分层扫描：
    1. market_regime 判断BTC环境+筛选Top币
    2. 对Top币逐一检查RSI/布林带入场时机
    返回 (signals, btc_env)
    """
    # 第1-2层：BTC环境 + 综合评分筛选
    top_coins, btc_env = scan_top_coins(
        min_volume_usdt=500000,
        max_coins=100,  # 全量扫描
        top_n=30,  # 取Top30评分币，再检查RSI信号
    )

    regime = btc_env.get('regime', 'unknown')
    bias = btc_env.get('bias', 0)
    direction = btc_env.get('direction', 'both')
    logger.info(f'📊 BTC环境: {regime} (bias={bias:+d}) → 方向: {direction}')

    # 取评分最高的30个币的symbol
    top_symbols = [r['symbol'] for r in top_coins if r['symbol'] not in BLOCKLIST]
    if not top_symbols:
        return [], btc_env

    # ─── 预取 BTC 1m K线用于相对强度计算 ───
    btc_returns = {}
    try:
        btc_1m = _fetch_klines_ws('BTCUSDT', '1m', 10)
        if btc_1m is not None and len(btc_1m) >= 8:
            btc_close = btc_1m['close'].values
            btc_now = float(btc_close[-1])
            btc_returns['1m'] = (btc_now - float(btc_close[-2])) / float(btc_close[-2]) * 100 if len(btc_close) >= 2 else 0
            btc_returns['3m'] = (btc_now - float(btc_close[-4])) / float(btc_close[-4]) * 100 if len(btc_close) >= 4 else 0
            btc_returns['5m'] = (btc_now - float(btc_close[0])) / float(btc_close[0]) * 100 if len(btc_close) >= 5 else 0
    except:
        pass

    # 第3层：5m 评分制入场（硬拒绝 + 动态评分 0-15）
    def _compute_signal(sym):
        try:
            df = _fetch_klines_ws(sym, TIMEFRAME, KLINES_LIMIT)
            if df is None or df.empty or len(df) < 30:
                return None
            df = compute_indicators(df)
            last = df.iloc[-1]
            prev = df.iloc[-2]

            close = float(last['close'])
            ema9 = float(last.get('ema9', close))
            ema21 = float(last.get('ema21', close))
            rsi = float(last.get('rsi', 50))
            vol = float(last.get('volume', 0))
            vol_avg = float(last.get('vol_avg', 1))
            atr = float(last.get('atr', close * 0.005))
            swing_low = float(last.get('swing_low', close * 0.99))
            swing_high = float(last.get('swing_high', close * 1.01))
            prev_close = float(prev['close'])
            prev_ema9 = float(prev.get('ema9', prev_close))

            trend_up = ema9 > ema21
            trend_down = ema9 < ema21
            vol_ratio = vol / vol_avg if vol_avg > 0 else 1
            ema_dist_pct = abs(close - ema9) / ema9 * 100
            cfg = _ENTRY_CFG

            # ─── 硬拒绝 + 防追高 ───
            if sym in BLOCKLIST:
                return None

            # 暴涨冷却检查
            now_ts = time.time()
            if sym in _PUMP_COOLDOWNS and now_ts < _PUMP_COOLDOWNS[sym]:
                remaining = int(_PUMP_COOLDOWNS[sym] - now_ts)
                if remaining > 0:
                    return None  # POST_PUMP_COOLDOWN

            # 获取1m K线检测暴涨
            df_1m = None
            try:
                df_1m = _fetch_klines_ws(sym, '1m', 8)
            except:
                pass

            pump_detected = False
            pump_reason = ''
            if df_1m is not None and len(df_1m) >= 5:
                close_1m = df_1m['close'].values
                atr_ref = max(atr, close * 0.003)  # 最小0.3%波动

                pct_1m = abs(close - close_1m[-2]) / close_1m[-2] * 100 if len(close_1m) >= 2 else 0
                pct_3m = abs(close - close_1m[-4]) / close_1m[-4] * 100 if len(close_1m) >= 4 else 0
                pct_5m = abs(close - close_1m[0]) / close_1m[0] * 100 if len(close_1m) >= 5 else 0

                atr_pct = atr_ref / close * 100
                max_1m = max(atr_pct * cfg.get('max_1m_move_atr', 0.8), 0.4)
                max_3m = max(atr_pct * cfg.get('max_3m_move_atr', 1.5), 0.8)
                max_5m = max(atr_pct * cfg.get('max_5m_move_atr', 2.5), 1.2)

                if pct_1m > max_1m:
                    pump_detected = True
                    pump_reason = f'1m泵{pct_1m:.1f}%>{max_1m:.1f}%'
                elif pct_3m > max_3m:
                    pump_detected = True
                    pump_reason = f'3m泵{pct_3m:.1f}%>{max_3m:.1f}%'
                elif pct_5m > max_5m:
                    pump_detected = True
                    pump_reason = f'5m泵{pct_5m:.1f}%>{max_5m:.1f}%'

            if pump_detected:
                cooldown_s = cfg.get('post_pump_cooldown_minutes', 5) * 60
                _PUMP_COOLDOWNS[sym] = now_ts + cooldown_s
                return None  # POST_PUMP_COOLDOWN

            # ─── 1m M顶/W底检测（防反转陷阱）───
            m_top = False; w_bottom = False; pattern_risk = 0
            if df_1m is not None and len(df_1m) >= 6:
                highs = df_1m['high'].values[-6:]
                lows = df_1m['low'].values[-6:]
                closes = df_1m['close'].values[-6:]
                mid = closes[-1]
                # M顶: 两个几乎等高的峰，中间有低点，当前价格在回落中
                peaks = []
                for i in range(1, len(highs)-1):
                    if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                        if highs[i] > mid * 1.002:
                            peaks.append((i, highs[i]))
                if len(peaks) >= 2:
                    p1, p2 = peaks[-2], peaks[-1]
                    p1_idx, p1_h = p1; p2_idx, p2_h = p2
                    if p2_idx - p1_idx >= 2:  # 两个峰之间至少隔一根K线
                        between_lows = lows[p1_idx+1:p2_idx]
                        valley = min(between_lows) if len(between_lows) > 0 else p1_h
                        pct_diff = abs(p2_h - p1_h) / p1_h
                        valley_pct = (p1_h - valley) / p1_h
                        if pct_diff < 0.01 and valley_pct > 0.003:  # 双峰差距<1%，谷深>0.3%
                            m_top = True
                            pattern_risk = 3
                # W底: 两个几乎等高的谷，中间有峰，当前价格在反弹中
                if not m_top:
                    valleys = []
                    for i in range(1, len(lows)-1):
                        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                            if lows[i] < mid * 0.998:
                                valleys.append((i, lows[i]))
                    if len(valleys) >= 2:
                        v1, v2 = valleys[-2], valleys[-1]
                        v1_idx, v1_l = v1; v2_idx, v2_l = v2
                        if v2_idx - v1_idx >= 2:
                            between_highs = highs[v1_idx+1:v2_idx]
                            peak = max(between_highs) if len(between_highs) > 0 else v1_l
                            pct_diff = abs(v2_l - v1_l) / v1_l
                            peak_pct = (peak - v1_l) / v1_l
                            if pct_diff < 0.01 and peak_pct > 0.003:
                                w_bottom = True
                                pattern_risk = 3

            # VWAP/EMA20 距离过滤（宽松硬拒 + 打分）
            vwap = float(last.get('vwap', close))
            ema20 = float(last.get('ema20', close))
            dist_vwap = abs(close - vwap) / close
            dist_ema20 = abs(close - ema20) / close

            # 硬拒：极端偏离（>1.5x ATR）
            max_vwap_hard = 1.5 * (atr / close)
            max_ema20_hard = 1.2 * (atr / close)
            if dist_vwap > max_vwap_hard:
                return None  # 离VWAP太远
            if dist_ema20 > max_ema20_hard:
                return None  # 离EMA20太远

            if ema_dist_pct > cfg['ema_hard']:
                return None
            if vol_ratio < cfg['vol_reject']:
                return None

            # ─── 方向判断（BTC偏置，非唯一方向）───
            # 允许双向开仓，BTC只控制风险系数
            long_allowed = trend_up
            short_allowed = trend_down
            if not long_allowed and not short_allowed:
                return None

            side = 'LONG' if long_allowed else 'SHORT'
            if long_allowed and short_allowed:
                side = 'LONG' if bias >= 0 else 'SHORT'

            # BTC方向偏置：同向正常，反向降低风险
            btc_align = (side == 'LONG' and bias >= 0) or (side == 'SHORT' and bias <= 0)
            direction_risk_factor = 1.0 if btc_align else 0.6

            if side == 'LONG':
                rsi_ideal = cfg['rsi_l_ideal']
                rsi_ok = cfg['rsi_l_ok']
            else:
                rsi_ideal = cfg['rsi_s_ideal']
                rsi_ok = cfg['rsi_s_ok']

            score = 0.0

            # 1. 趋势结构 (max 4.0)
            if trend_up:
                if close > ema21: score += 1.0  # 价格在EMA21之上
                if ema9 > ema21: score += 1.0  # EMA9在EMA21之上
            else:
                if close < ema21: score += 1.0
                if ema9 < ema21: score += 1.0
            # 短周期趋势确认 (前一根K线也同向)
            if prev_ema9 > ema21 and ema9 > ema21:
                score += 1.0  # 持续多头
            elif prev_ema9 < ema21 and ema9 < ema21:
                score += 1.0  # 持续空头
            else:
                score += 0.5  # 刚转向
            if ema_dist_pct <= cfg['ema_max']:
                score += 1.0  # 价格在合理趋势区域内

            # 2. EMA回踩质量 (max 2.0)
            if ema_dist_pct <= cfg['ema_ideal']:
                score += 2.0
            elif ema_dist_pct <= cfg['ema_normal']:
                score += 1.5
            elif ema_dist_pct <= cfg['ema_max']:
                score += 1.0

            # 3. RSI (max 2.0)
            if rsi_ideal[0] <= rsi <= rsi_ideal[1]:
                score += 2.0
            elif rsi_ok[0] <= rsi <= rsi_ok[1]:
                score += 1.0
            elif rsi > rsi_ok[1] - 2 and rsi <= rsi_ok[1] + 2:
                score += 0.5
            elif rsi < rsi_ok[0] + 2 and rsi >= rsi_ok[0] - 2:
                score += 0.5

            # 3b. MACD (max 2.5, 仅加分不扣分)
            try:
                macd = float(last.get('macd', 0))
                macd_sig = float(last.get('macd_signal', 0))
                macd_hist = float(last.get('macd_histogram', 0))
                prev_macd = float(prev.get('macd', 0))
                prev_sig = float(prev.get('macd_signal', 0))
                # 金叉
                if prev_macd <= prev_sig and macd > macd_sig:
                    score += 1.5
                # 零轴上方
                if macd > 0:
                    score += 0.5
                # 柱状图放大
                if abs(macd_hist) > abs(float(prev.get('macd_histogram', 0))):
                    score += 0.5
            except Exception:
                pass

            # 4. 当前K线确认 (max 2.5)
            if side == 'LONG':
                if close > prev_close: score += 1.0
                if close > prev_ema9: score += 1.0
            else:
                if close < prev_close: score += 1.0
                if close < prev_ema9: score += 1.0
            # 影线确认
            low_p = float(last.get('low', close))
            high_p = float(last.get('high', close))
            if side == 'LONG' and (close - low_p) > (high_p - close) * 0.5:
                score += 0.5  # 下影线明显
            elif side == 'SHORT' and (high_p - close) > (close - low_p) * 0.5:
                score += 0.5  # 上影线明显

            # 5. 成交量 (max 2.0)
            if vol_ratio >= cfg['vol_strong']:
                score += 2.0
            elif vol_ratio >= cfg['vol_normal']:
                score += 1.5
            elif vol_ratio >= cfg['vol_reject']:
                score += 1.0

            # 6. BTC环境 (max 1.5)
            if side == 'LONG' and bias >= 4: score += 1.5
            elif side == 'LONG' and bias >= 1: score += 0.5
            elif side == 'SHORT' and bias <= -4: score += 1.5
            elif side == 'SHORT' and bias <= -1: score += 0.5
            else: score += 0.5  # BTC中性

            # 7. 相对强度 vs BTC (max 1.0)
            # 计算山寨币自身 1m/3m/5m 收益，与 BTC 对比
            rs_score = 0.0
            rs_3m = 0.0
            rs_5m = 0.0
            if btc_returns:
                try:
                    coin_1m = _fetch_klines_ws(sym, '1m', 10)
                    if coin_1m is not None and len(coin_1m) >= 8:
                        coin_close = coin_1m['close'].values
                        coin_now = float(coin_close[-1])
                        coin_1m_ret = (coin_now - float(coin_close[-2])) / float(coin_close[-2]) * 100 if len(coin_close) >= 2 else 0
                        coin_3m_ret = (coin_now - float(coin_close[-4])) / float(coin_close[-4]) * 100 if len(coin_close) >= 4 else 0
                        coin_5m_ret = (coin_now - float(coin_close[0])) / float(coin_close[0]) * 100 if len(coin_close) >= 5 else 0

                        rs_3m = coin_3m_ret - btc_returns.get('3m', 0)
                        rs_5m = coin_5m_ret - btc_returns.get('5m', 0)

                        if rs_3m > 0:
                            rs_score += 1.0
                        if rs_5m > 0:
                            rs_score += 0.5
                except:
                    pass

            # 硬拒绝：BTC 涨但山寨跌（弱势山寨，不做多）
            if side == 'LONG' and btc_returns and btc_returns.get('3m', 0) > 0.05 and rs_3m < -0.30:
                return None  # BTC上涨但山寨显著落后
            if side == 'SHORT' and btc_returns and btc_returns.get('3m', 0) < -0.05 and rs_3m > 0.30:
                return None  # BTC下跌但山寨显著抗跌

            score += rs_score

            # 8. 支撑区质量 (max 2.0) — 禁止EMA9单独当支撑
            # 价格必须至少靠近 VWAP 或 EMA20 中的至少一个
            atr_pct = atr / close
            near_vwap = dist_vwap <= 0.5 * atr_pct
            near_ema20 = dist_ema20 <= 0.4 * atr_pct
            near_swing = (close - swing_low) / close <= 1.5 * atr_pct if side == 'LONG' else (swing_high - close) / close <= 1.5 * atr_pct

            if near_vwap: score += 0.8
            if near_ema20: score += 0.7
            # VWAP和EMA20收敛（两者距离 < 0.25*ATR）
            if abs(vwap - ema20) / close < 0.25 * atr_pct:
                score += 0.5
            # 硬拒绝：EMA9距离很近但VWAP/EMA20都很远 = 假支撑
            if ema_dist_pct <= 0.15 and not near_vwap and not near_ema20:
                score -= 1.5  # EMA9孤立，不是可靠支撑

            # 硬拒绝：离前高太近（没有上涨空间）
            if side == 'LONG':
                dist_from_high = (swing_high - close) / close
                if dist_from_high < 0.3 * atr_pct:
                    return None  # 太接近前高，不追
            else:
                dist_from_low = (close - swing_low) / close
                if dist_from_low < 0.3 * atr_pct:
                    return None  # 太接近前低，不追

            # ─── 位置百分位 + 极值惩罚 ───
            pos_pct = (close - swing_low) / (swing_high - swing_low) if swing_high > swing_low else 0.5
            extreme_penalty = 0
            if side == 'LONG':
                if pos_pct > 0.90: extreme_penalty = 20
                elif pos_pct > 0.80: extreme_penalty = 12
                elif pos_pct > 0.70: extreme_penalty = 5
            else:
                if pos_pct < 0.10: extreme_penalty = 20
                elif pos_pct < 0.20: extreme_penalty = 12
                elif pos_pct < 0.30: extreme_penalty = 5

            # ─── VWAP偏离硬限制 ───
            vwap_dev_atr = (close - vwap) / atr if atr > 0 else 0
            if side == 'LONG' and vwap_dev_atr > 1.2:
                return None  # 价格远高于VWAP，不追多
            if side == 'SHORT' and vwap_dev_atr < -1.2:
                return None  # 价格远低于VWAP，不追空

            # ─── 动量衰竭检测 ───
            momentum_exhausted = False

            # ─── 区间中部禁开（震荡时更严格）───
            if 0.35 < pos_pct < 0.65:
                extreme_penalty += 8  # 区间中部，额外扣分

            # ─── 回踩质量评估 ───
            pullback_bars = 0
            push_bars = 0
            pullback_vol = 0.0
            push_vol = 0.0
            for i in range(-8, 0):
                ci = float(df.iloc[i]['close'])
                pi = float(df.iloc[i-1]['close']) if i > -len(df) else ci
                vi = float(df.iloc[i]['volume'])
                if ci < pi:
                    pullback_bars += 1
                    pullback_vol += vi
                else:
                    push_bars += 1
                    push_vol += vi

            min_pb_bars = cfg.get('min_pullback_bars', 2)
            max_pb_bars = cfg.get('max_pullback_bars', 8)

            if pullback_bars < min_pb_bars:
                score -= 1.5  # 回调太短
            elif pullback_bars > max_pb_bars:
                score -= 1.0  # 回调太久，趋势可能失效

            # 回撤缩量检查
            if pullback_bars > 0 and push_bars > 0:
                pb_vol_avg = pullback_vol / pullback_bars
                push_vol_avg = push_vol / push_bars
                if push_vol_avg > 0:
                    pb_vol_ratio = pb_vol_avg / push_vol_avg
                    if pb_vol_ratio < cfg.get('max_pullback_volume_ratio', 0.70):
                        score += 1.0  # 健康缩量
                    elif pb_vol_ratio > 1.2:
                        score -= 1.0  # 放量回调，可能是抛压

            # Higher Low检测
            recent_lows = []
            for i in range(-10, 0):
                li = float(df.iloc[i]['low'])
                recent_lows.append(li)
            hl_count = 0
            for i in range(1, len(recent_lows)-1):
                if recent_lows[i] > recent_lows[i+1]:
                    hl_count += 1
            if hl_count >= 2:
                score += 1.0  # 连续Higher Low

            # ─── 动量衰竭检测（回踩分析完成后）───
            if not momentum_exhausted and pullback_bars > 0 and push_bars > 0:
                if vol_ratio < 0.6 and side == 'LONG' and close > prev_close:
                    momentum_exhausted = True
                elif vol_ratio < 0.6 and side == 'SHORT' and close < prev_close:
                    momentum_exhausted = True

            if momentum_exhausted:
                score -= 3.0

            # ─── 极值位置惩罚 + 区间中部 ───
            score -= extreme_penalty

            # ─── 入场分层 ───
            # 评分≥7.0 + 位置有利(20%~65%LONG/35%~80%SHORT) → 市价
            # 评分≥7.0 + 位置不利 → 激进LIMIT(30s过期)
            # 评分≥6.0 → 激进LIMIT(30s)
            # 评分≥4.5 → 被动LIMIT(90s)
            tier = 'skip'
            if score >= cfg['market']:
                if side == 'LONG' and pos_pct > 0.65:
                    tier = 'aggressive'  # 位置太右，降级防追高
                elif side == 'SHORT' and pos_pct < 0.35:
                    tier = 'aggressive'
                else:
                    tier = 'market'
            if tier == 'skip' and score >= cfg['aggressive']:
                tier = 'aggressive'
            if tier == 'skip' and score >= cfg['limit']:
                tier = 'limit'

            if tier == 'skip':
                return None  # 不够最低门槛

            # ─── 结构止损计算（多锚点）───
            # 计算回撤最低点（用于止损锚定）
            pullback_low = close
            pullback_high = close
            for i in range(-pullback_bars, 0):
                li = float(df.iloc[i]['low'])
                hi = float(df.iloc[i]['high'])
                if li < pullback_low: pullback_low = li
                if hi > pullback_high: pullback_high = hi

            if side == 'LONG':
                limit_price = max(ema9, close * 0.998)
                # 结构止损: 取最保守锚点
                sl_buf_5m = cfg.get('atr_5m_buffer', 0.25)
                min_buf = cfg.get('minimum_buffer_pct', 0.001) * close
                atr_buf = max(atr * sl_buf_5m, min_buf)
                structural_sl = min(pullback_low, swing_low, ema21 - atr * 0.20)
                sl_price = structural_sl - atr_buf
                sl_price = round(sl_price, 8)
                risk_dist = abs(limit_price - sl_price)
                tp_raw = max(swing_high, limit_price + risk_dist * 2)
                tp_price = round(min(max(tp_raw, limit_price + risk_dist * 1.5), limit_price * 1.03), 8)
            else:
                limit_price = min(ema9, close * 1.001)
                sl_buf_5m = cfg.get('atr_5m_buffer', 0.25)
                min_buf = cfg.get('minimum_buffer_pct', 0.001) * close
                atr_buf = max(atr * sl_buf_5m, min_buf)
                structural_sl = max(pullback_high, swing_high, ema21 + atr * 0.20)
                sl_price = structural_sl + atr_buf
                sl_price = round(sl_price, 8)
                risk_dist = abs(sl_price - limit_price)
                tp_raw = min(swing_low, limit_price - risk_dist * 2)
                tp_price = round(max(min(tp_raw, limit_price - risk_dist * 1.5), limit_price * 0.97), 8)

            detail = f'EMA{ema_dist_pct:.1f}% VWAP{dist_vwap*100:.1f}% PB{pullback_bars}b RSI{rsi:.0f}'
            early = score < cfg['aggressive'] and cfg['early_enabled']
            mode = 'early' if early else ('confirmed' if score >= cfg['aggressive'] else 'momentum')
            emoji = '🟢' if side == 'LONG' else '🔴'

            logger.info(f'  {emoji} {sym} {side} [{tier}|{mode}] sc={score:.1f} {detail}')

            # ─── 四维子分 ───
            # direction_score (0-10): 趋势结构 + BTC/ETH + RS + 成交量方向
            dir_score = 0.0
            # 趋势结构部分 (来自原始评分项1, max 4)
            if side == 'LONG':
                if close > ema21: dir_score += 1.0
                if ema9 > ema21: dir_score += 1.0
            else:
                if close < ema21: dir_score += 1.0
                if ema9 < ema21: dir_score += 1.0
            if ema_dist_pct <= cfg['ema_max']: dir_score += 1.0
            # BTC bonus (max 1.5)
            if (side=='LONG' and bias>=4) or (side=='SHORT' and bias<=-4): dir_score += 1.5
            elif (side=='LONG' and bias>=1) or (side=='SHORT' and bias<=-1): dir_score += 0.5
            else: dir_score += 0.5
            # RS (max 1.5)
            dir_score += min(1.5, rs_score)
            # 成交量方向
            if vol_ratio >= cfg['vol_strong']: dir_score += 1.5
            elif vol_ratio >= cfg['vol_normal']: dir_score += 1.0
            elif vol_ratio >= cfg['vol_reject']: dir_score += 0.5
            dir_score = round(min(10, dir_score), 1)

            # location_score (0-10): 位置百分位 + VWAP偏离 + 支撑距离 + 空间
            loc_score = 0.0
            # 位置百分位 (max 2.5)
            if side == 'LONG':
                if pos_pct <= 0.20: loc_score += 2.5
                elif pos_pct <= 0.35: loc_score += 2.0
                elif pos_pct <= 0.50: loc_score += 1.0
            else:
                if pos_pct >= 0.80: loc_score += 2.5
                elif pos_pct >= 0.65: loc_score += 2.0
                elif pos_pct >= 0.50: loc_score += 1.0
            # VWAP偏离 (max 2.0)
            vwap_dist_pct = abs(close-vwap)/close*100
            if vwap_dist_pct <= 0.3: loc_score += 2.0
            elif vwap_dist_pct <= 0.6: loc_score += 1.0
            elif vwap_dist_pct <= 1.0: loc_score += 0.5
            # 支撑/压力距离 (max 2.0)
            if near_vwap: loc_score += 1.0
            if near_ema20: loc_score += 1.0
            # Z-score位置 (max 1.5)
            sma20 = float(last.get('sma20', close))
            bb_std = float(last.get('bb_std', atr))
            z = (close - sma20) / bb_std if bb_std > 0 else 0
            if side == 'LONG' and z < -0.5: loc_score += 1.5
            elif side == 'LONG' and z < 0: loc_score += 0.8
            elif side == 'SHORT' and z > 0.5: loc_score += 1.5
            elif side == 'SHORT' and z > 0: loc_score += 0.8
            # 边界触碰次数 (max 1.0) — 简化：首次靠近给满分
            if abs(close - swing_low) / close < atr_pct if side=='LONG' else abs(swing_high - close) / close < atr_pct:
                loc_score += 1.0
            loc_score = round(min(10, loc_score), 1)

            # trigger_score (0-10): K线确认 + 回踩质量 + Higher Low + 缩量
            trig_score = 0.0
            # K线确认 (max 2.0, from original items 4)
            if side == 'LONG':
                if close > prev_close: trig_score += 1.0
                if close > prev_ema9: trig_score += 1.0
            else:
                if close < prev_close: trig_score += 1.0
                if close < prev_ema9: trig_score += 1.0
            # 回踩质量 (max 2.0)
            if pullback_bars >= 2 and pullback_bars <= 6: trig_score += 2.0
            elif pullback_bars > 0: trig_score += 1.0
            # Higher Low (max 1.5)
            if hl_count >= 2: trig_score += 1.5
            elif hl_count >= 1: trig_score += 0.8
            # 回踩缩量 (max 1.5)
            if pullback_bars > 0 and push_bars > 0:
                pb_vol_ratio2 = (pullback_vol/pullback_bars) / (push_vol/push_bars) if push_vol > 0 else 999
                if pb_vol_ratio2 < 0.55: trig_score += 1.5
                elif pb_vol_ratio2 < 0.75: trig_score += 1.0
                elif pb_vol_ratio2 < 1.0: trig_score += 0.5
            # RSI拐头 (max 1.0)
            if side == 'LONG' and rsi > float(prev.get('rsi', rsi)): trig_score += 1.0
            elif side == 'SHORT' and rsi < float(prev.get('rsi', rsi)): trig_score += 1.0
            trig_score = round(min(10, trig_score), 1)

            # execution_score (0-10): 止损合理 + 箱体合适
            exec_score = 0.0
            stop_pct = abs(sl_price - limit_price) / limit_price * 100
            if 0.45 <= stop_pct <= 0.85: exec_score += 4.0
            elif 0.45 <= stop_pct <= 1.10: exec_score += 2.0
            # 盈亏比 (max 3.0)
            rr = abs(tp_price - limit_price) / max(0.001, abs(sl_price - limit_price))
            if rr >= 1.8: exec_score += 3.0
            elif rr >= 1.4: exec_score += 2.0
            elif rr >= 1.0: exec_score += 1.0
            # 位置合适 (max 3.0)
            if side == 'LONG' and pos_pct <= 0.50: exec_score += 3.0
            elif side == 'LONG' and pos_pct <= 0.65: exec_score += 1.5
            elif side == 'SHORT' and pos_pct >= 0.50: exec_score += 3.0
            elif side == 'SHORT' and pos_pct >= 0.35: exec_score += 1.5
            exec_score = round(min(10, exec_score), 1)

            # ─── 1m形态风险加减分 ───
            if pattern_risk > 0:
                if m_top and side == 'LONG':
                    score -= pattern_risk
                    detail += ' M顶!'
                elif m_top and side == 'SHORT':
                    score += 2  # M顶确认空头方向
                    detail += ' M顶✅'
                if w_bottom and side == 'SHORT':
                    score -= pattern_risk
                    detail += ' W底!'
                elif w_bottom and side == 'LONG':
                    score += 2  # W底确认多头方向
                    detail += ' W底✅'

            return {
                'symbol': sym, 'side': side, 'score': round(score, 1),
                'tier': tier, 'mode': mode, 'rsi': round(rsi, 1),
                'reason': f'{tier}/{mode} {detail}',
                'limit_price': round(limit_price, 8),
                'sl_price': sl_price, 'tp_price': tp_price,
                'early': early,
                'dist_vwap': round(dist_vwap * 100, 2),
                'dist_ema20': round(dist_ema20 * 100, 2),
                'pullback_bars': pullback_bars,
                'direction_risk_factor': direction_risk_factor,
                'pos_pct': round(pos_pct, 3),
                'dir_score': dir_score,
                'loc_score': loc_score,
                'trig_score': trig_score,
                'exec_score': exec_score,
            }
        except Exception:
            pass
        return None

    # 串行检查信号
    signals = []
    for sym in top_symbols:
        result = _compute_signal(sym)
        if result:
            signals.append(result)

    signals.sort(key=lambda x: x['score'], reverse=True)
    if signals:
        logger.info(f'  信号 {len(signals)}个，最优: {signals[0]["symbol"]} {signals[0]["side"]}'
                    f' (评分{signals[0]["score"]})')
    else:
        logger.info('  无入场信号')
    # ─── 概率型行情识别 ───
    regime_probs = detect_regime_probabilities(btc_env, top_coins)
    btc_env['regime_probs'] = regime_probs

    return signals[:3], btc_env


# ═══════════════════════════════════════════════════
#  限价单成交检测
# ═══════════════════════════════════════════════════

def run_scalper():
    logger.info('🏃‍♂️ 舔一口策略启动')

    # 第-1步：同步交易所孤儿仓到 bot 状态
    logger.info('  ── 孤儿仓同步 ──')
    _sync_orphan_positions()

    # 第0步：检测 pending 限价单是否已成交
    logger.info('  ── 限价单成交检测 ──')
    _sync_pending_orders()

    # 第0.5步：趋势反查
    logger.info('  ── 趋势反查 ──')
    _review_scalp_positions()

    # 不用条件单，跳过清理
    logger.info('  ── 跳过条件单清理 ──')

    # 不再挂交易所条件单，由 position_manager WS 监控负责 SL/TP
    logger.info('  ── 跳过条件单检查 (WS本地监控) ──')

    state = load_bot_state()
    scalp_positions = {k: v for k, v in state.get('positions', {}).items()
                       if v.get('strategy') == 'scalp'}
    current_scalp_count = len(scalp_positions)

    logger.info(f'📊 当前超短线单: {current_scalp_count}/{SCALP_MAX_POSITIONS}')

    # ── 日亏损熔断检查 ──
    can_trade, limit_reason = check_daily_limit()
    if not can_trade:
        logger.warning(f'🛑 日亏损熔断: {limit_reason}')
        _check_trading_status(False, [limit_reason])
        return

    if current_scalp_count >= SCALP_MAX_POSITIONS:
        logger.info('⏸️ 超短线仓位已满，跳过')
        _check_trading_status(False, [f'超短线仓位已满 {current_scalp_count}/{SCALP_MAX_POSITIONS}'])
        return

    # ── 垃圾单轮换：满仓但有高分信号 → 踢掉最低分 ──
    try:
        total_pos = _fetch_total_positions()
        if total_pos >= MAX_TOTAL_POSITIONS:
            # 获取得分最高的新信号
            _signals, _ = scan_signals()
            if not _signals:
                logger.info(f'⏸️ 总仓位已达上限 {total_pos}/{MAX_TOTAL_POSITIONS}，无新信号，跳过')
                _check_trading_status(False, [f'总仓位已达上限 {total_pos}/{MAX_TOTAL_POSITIONS}，无高分轮换信号'])
                return
            best_new = _signals[0]
            new_score = best_new.get('score', 0)
            if new_score < 8.5:
                ns = best_new['symbol']
                logger.info(f'⏸️ 总仓位已上限 {total_pos}/{MAX_TOTAL_POSITIONS}，最优信号{ns} {new_score:.1f}<8.5，跳过')
                _check_trading_status(False, [f'总仓位已达上限 {total_pos}/{MAX_TOTAL_POSITIONS}，最优信号{ns} {new_score:.1f}<8.5'])
                return
            
            # 对所有持仓垃圾评分
            state = load_bot_state()
            positions = state.get('positions', {})
            junk_list = []
            for key, pos in positions.items():
                if pos.get('status') != 'active':
                    continue
                sym = pos.get('symbol', key.split(':')[0])
                # get mark price
                mark = 0
                try:
                    bt = market_cache.get_book_ticker(sym)
                    mark = (bt['b'] + bt['a']) / 2 if bt else 0
                except:
                    pass
                js = _junk_score(pos, mark)
                junk_list.append((js, key, sym, pos))
            
            junk_list.sort(key=lambda x: x[0])  # 最低分排前
            if junk_list and junk_list[0][0] <= -3:
                js_score, j_key, j_sym, j_pos = junk_list[0]
                best_sym_new = best_new['symbol']
                logger.info(f'  🗑️ 轮换: {j_sym} (垃圾分{js_score}) → 让位给 {best_sym_new} ({new_score:.1f})')
                try:
                    from trading_bot.services.position_manager import market_close_position
                    qty = float(j_pos.get('qty', 0))
                    if qty > 0 and market_close_position(j_sym, j_pos.get('side', 'LONG'), qty):
                        # 计算PnL（不计入连损）
                        entry_p = float(j_pos.get('entry_price', 0))
                        try:
                            rot_bt = market_cache.get_book_ticker(j_sym)
                            rot_price = (rot_bt['b'] + rot_bt['a']) / 2 if rot_bt else entry_p
                        except:
                            rot_price = entry_p
                        rot_pnl = (rot_price - entry_p) * qty
                        record_trade_result(rot_pnl, is_rotation=True)
                        logger.info(f'  ✅ {j_sym} 已平仓，释放仓位')
                except Exception as e:
                    logger.warning(f'  ⚠️ 淘汰平仓失败: {e}')
                    return
            else:
                if junk_list:
                    best_junk = junk_list[-1][0]  # highest score = least junk
                    ns2 = best_new['symbol']
                    logger.info(f'⏸️ 总仓位已上限 {total_pos}/{MAX_TOTAL_POSITIONS}，最低垃圾分={junk_list[0][0]:.1f}(>-3)，不淘汰。新信号 {ns2} {new_score:.1f}')
                else:
                    logger.info(f'⏸️ 总仓位已上限 {total_pos}/{MAX_TOTAL_POSITIONS}，跳过')
                return
    except Exception as e:
        logger.warning(f'  ⚠️ 总仓位检查失败: {e}')
        return

    signals, btc_env = scan_signals()
    if not signals:
        logger.info('⏸️ 无入场信号')
        return


    # ── 全局市场结构检查（保护层）按真实方向 ──
    try:
        from trading_bot.strategy.market_structure import check_trade_permission
        best = signals[0] if signals else None
        if best:
            perm = check_trade_permission('GLOBAL', best['side'])
            if not perm.get('policy_allowed', perm.get('allowed', True)):
                logger.info(f'⏸️ 市场结构禁止开单: {perm["reason"]} (risk={perm["risk_factor"]})')
                return
            # 保存风险系数给开仓用
            _risk_factor = perm['risk_factor']
            if _risk_factor <= 0.3:
                logger.info(f'⚠️ 市场结构风险较低({_risk_factor:.1f})，谨慎开单: {perm["reason"]}')
    except Exception as e:
        logger.exception(f'  ❌ 市场结构保护层失败，禁止新开仓: {e}')
        return
    for signal_idx, best in enumerate(signals[:3]):
        if signal_idx > 0:
            state = load_bot_state()
            scalp_count = len([v for v in state.get('positions', {}).values()
                              if v.get('strategy') == 'scalp' and v.get('status') in ('active', 'pending')])
            if scalp_count >= SCALP_MAX_POSITIONS:
                return
        sym = best['symbol']
        side = best['side']
        logger.info(f'  入场信号[{signal_idx+1}]: {sym} {side} (BTC{btc_env["regime"]} bias={btc_env["bias"]:+d})')

        # ─── P0-1: 交易类型路由 ───
        pos_pct = best.get('pos_pct', 0.5)
        regime = btc_env.get('regime', 'unknown')

        # 分类: trend_pullback / range_reversal / momentum_scalp
        is_trend = regime in ('strong_bull', 'bull', 'mild_bull', 'strong_bear', 'bear', 'mild_bear')
        is_range = regime in ('range', 'CHOP', 'unknown')
        if is_trend:
            if 0.20 <= pos_pct <= 0.60:
                trade_type = 'TREND_PULLBACK'
            elif pos_pct < 0.20:
                trade_type = 'RANGE_REVERSAL'  # 低位反转
            else:
                trade_type = 'MOMENTUM_SCALP'  # 高位仅允许动量快单
        elif is_range:
            if pos_pct <= 0.25:
                trade_type = 'RANGE_REVERSAL'
            elif pos_pct >= 0.75:
                trade_type = 'RANGE_REVERSAL'
            else:
                logger.info(f'⏭️ {sym} CHOP区间中部 pos={pos_pct:.2f} 禁止开仓')
                continue
        else:
            # 过渡态/冷却期：只允许极值位置
            if pos_pct <= 0.20:
                trade_type = 'RANGE_REVERSAL'
            elif pos_pct >= 0.80:
                trade_type = 'RANGE_REVERSAL'
            else:
                return

        # ─── P0-5: 按交易模式四维门槛 ───
        thresholds = {
            'TREND_PULLBACK':    {'dir': 4.5, 'loc': 3.5, 'trig': 4.0, 'exec': 4.0},
            'RANGE_REVERSAL':    {'dir': 3.0, 'loc': 5.0, 'trig': 4.5, 'exec': 4.0},
            'MOMENTUM_SCALP':    {'dir': 4.5, 'loc': 3.0, 'trig': 5.5, 'exec': 5.0},
        }
        th = thresholds.get(trade_type, thresholds['TREND_PULLBACK'])

        dir_score = best.get('dir_score', 0)
        loc_score = best.get('loc_score', 0)
        trig_score = best.get('trig_score', 0)
        exec_score = best.get('exec_score', 0)

        reject_reason = None
        if dir_score < th['dir']: reject_reason = f'REJECT_LOW_DIR dir={dir_score:.1f}<{th["dir"]}'
        elif loc_score < th['loc']: reject_reason = f'REJECT_LOW_LOC loc={loc_score:.1f}<{th["loc"]}'
        elif trig_score < th['trig']: reject_reason = f'REJECT_LOW_TRIG trig={trig_score:.1f}<{th["trig"]}'
        elif exec_score < th['exec']: reject_reason = f'REJECT_LOW_EXEC exec={exec_score:.1f}<{th["exec"]}'
        elif side == 'LONG' and pos_pct > 0.75: reject_reason = f'REJECT_EXTREME_POS pos={pos_pct:.2f}'
        elif side == 'SHORT' and pos_pct < 0.25: reject_reason = f'REJECT_EXTREME_POS pos={pos_pct:.2f}'

        if reject_reason:
            logger.info(f'⏭️ {sym} [{trade_type}] {reject_reason}')
            continue

        # ─── P0-4: 按交易类型止损范围 ───
        stop_rules = {
            'TREND_PULLBACK':    {'min': 0.35, 'max': 0.90},
            'RANGE_REVERSAL':    {'min': 0.18, 'max': 0.45},
            'MOMENTUM_SCALP':    {'min': 0.12, 'max': 0.35},
        }
        stop_rule = stop_rules.get(trade_type, stop_rules['TREND_PULLBACK'])

        logger.info(f'  [{trade_type}] 四维: dir={dir_score:.1f} loc={loc_score:.1f} trig={trig_score:.1f} exec={exec_score:.1f} pos={pos_pct:.2f} stop={stop_rule["min"]:.2f}-{stop_rule["max"]:.2f}%')

        # 存储交易类型到 best
        best['trade_type'] = trade_type
        best['stop_min'] = stop_rule['min']
        best['stop_max'] = stop_rule['max']

        # ─── 订单流检查：点差+深度 ───
        try:
            import requests as _rq
            ob = _rq.get(f'{_FAPI_BASE}/depth',
                         params={'symbol': sym, 'limit': 5}, timeout=5)
            if ob.status_code == 200:
                data = ob.json()
                bids = data.get('bids', [])
                asks = data.get('asks', [])
                if bids and asks:
                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    spread_pct = (best_ask - best_bid) / best_bid * 100
                    bid_depth = sum(float(b[1]) for b in bids[:3])
                    ask_depth = sum(float(a[1]) for a in asks[:3])
                    if spread_pct > 0.15:
                        logger.info(f'⏭️ {sym} 点差{spread_pct:.3f}%>0.15% 过大')
                        return
                    logger.debug(f'  orderbook: spread={spread_pct:.3f}% bid_depth={bid_depth:.0f} ask_depth={ask_depth:.0f}')
        except Exception:
            pass

        # ─── 追价保护：当前价偏离计划入场价 > 0.15R 取消 ───
        try:
            ticker_now = fetch_ticker(None, sym)
            if ticker_now and ticker_now.get('last'):
                current_price = float(ticker_now['last'])
                plan_price = best.get('limit_price', current_price)
                stop_dist = best.get('sl_price', plan_price * 0.99)
                risk_dist = abs(plan_price - stop_dist)
                price_deviation = abs(current_price - plan_price)
                if risk_dist > 0 and price_deviation > 0.15 * risk_dist:
                    logger.info(f'⏭️ {sym} 追价偏差{price_deviation/risk_dist*100:.0f}%R > 15%R 取消')
                    return
        except Exception:
            pass

        # ─── 动态四维权重（按行情概率加权）───
        regime_probs = btc_env.get('regime_probs', {})
        if regime_probs:
            # 趋势环境权重
            trend_weight = {
                'dir': 0.35, 'loc': 0.25, 'trig': 0.25, 'exec': 0.15,
            }
            # 震荡/CHOP 权重
            range_weight = {
                'dir': 0.10, 'loc': 0.40, 'trig': 0.35, 'exec': 0.15,
            }
            # 高波动权重
            hvol_weight = {
                'dir': 0.20, 'loc': 0.20, 'trig': 0.25, 'exec': 0.35,
            }
            # 默认（切换/低活跃）权重
            default_weight = {
                'dir': 0.20, 'loc': 0.30, 'trig': 0.30, 'exec': 0.20,
            }

            # 按概率混合权重
            w_dir = (regime_probs.get('TREND_UP',0) + regime_probs.get('TREND_DOWN',0)) * trend_weight['dir']
            w_dir += regime_probs.get('RANGE',0) * range_weight['dir']
            w_dir += regime_probs.get('HIGH_VOLATILITY',0) * hvol_weight['dir']
            w_dir += (regime_probs.get('TRANSITION',0) + regime_probs.get('LOW_ACTIVITY',0)) * default_weight['dir']

            w_loc = (regime_probs.get('TREND_UP',0) + regime_probs.get('TREND_DOWN',0)) * trend_weight['loc']
            w_loc += regime_probs.get('RANGE',0) * range_weight['loc']
            w_loc += regime_probs.get('HIGH_VOLATILITY',0) * hvol_weight['loc']
            w_loc += (regime_probs.get('TRANSITION',0) + regime_probs.get('LOW_ACTIVITY',0)) * default_weight['loc']

            w_trig = (regime_probs.get('TREND_UP',0) + regime_probs.get('TREND_DOWN',0)) * trend_weight['trig']
            w_trig += regime_probs.get('RANGE',0) * range_weight['trig']
            w_trig += regime_probs.get('HIGH_VOLATILITY',0) * hvol_weight['trig']
            w_trig += (regime_probs.get('TRANSITION',0) + regime_probs.get('LOW_ACTIVITY',0)) * default_weight['trig']

            w_exec = (regime_probs.get('TREND_UP',0) + regime_probs.get('TREND_DOWN',0)) * trend_weight['exec']
            w_exec += regime_probs.get('RANGE',0) * range_weight['exec']
            w_exec += regime_probs.get('HIGH_VOLATILITY',0) * hvol_weight['exec']
            w_exec += (regime_probs.get('TRANSITION',0) + regime_probs.get('LOW_ACTIVITY',0)) * default_weight['exec']

            weighted_score = dir_score * w_dir + loc_score * w_loc + trig_score * w_trig + exec_score * w_exec
            logger.info(f'  动态权重: w=({w_dir:.2f},{w_loc:.2f},{w_trig:.2f},{w_exec:.2f}) → weighted={weighted_score:.1f}')

        # 双重检查：bot_state + 交易所实时持仓
        if position_key(sym, side) in state.get('positions', {}):
            logger.info(f'⏭️ {sym} 已有持仓(bot_state)，跳过')
            continue
        # 交易所实时持仓检查
        try:
            all_positions = _gw.get_positions()
            for p in all_positions:
                if p.symbol == sym and abs(float(p.position_amt)) > 0:
                    logger.info(f'⏭️ {sym} 已有持仓(交易所)，跳过')
                    return
        except Exception as e:
            logger.warning(f'  ⚠️ 交易所持仓检查失败: {e}')

        # ─── 分层入场执行 ───
        tier = best.get('tier', 'limit')
        score = best.get('score', 0)
        limit_price = best.get('limit_price', 0)
        early = best.get('early', False)

        use_market = tier == 'market'
        use_aggressive = tier == 'aggressive'

        try:
            _api('POST', 'leverage', {'symbol': sym, 'leverage': SCALP_LEVERAGE})

            from trading_bot.exchange.client import _get_symbol_precision, _load_precisions
            from trading_bot.exchange.market_data import fetch_ticker
            _load_precisions()
            qty_decimals, step, price_decimals, tick = _get_symbol_precision(sym)

            # 风险系数：market=1.0, aggressive=0.8, limit=0.6, early=0.5
            if early:
                market_risk = _ENTRY_CFG['early_risk']
            elif use_market:
                market_risk = 1.0
            elif use_aggressive:
                market_risk = 0.8
            else:
                market_risk = 0.6

            effective_risk = _risk_factor * market_risk * best.get('direction_risk_factor', 1.0)

            if use_market:
                ticker = fetch_ticker(None, sym)
                entry_ref = ticker['last'] if ticker else limit_price
                logger.info(f'🚀 市价入场 {sym} {side} tier={tier} sc={score}')
            elif use_aggressive:
                # 激进LIMIT：贴近当前价
                ticker = fetch_ticker(None, sym)
                if ticker and ticker.get('last'):
                    if side == 'LONG':
                        limit_price = min(ticker['last'] * 1.001, limit_price)
                    else:
                        limit_price = min(ticker['last'] * 1.0005, limit_price)
                    limit_price = round(limit_price, price_decimals)
                entry_ref = limit_price
                effective_risk *= 0.8
                logger.info(f'🚀 激进LIMIT {sym} {side} @{limit_price} tier={tier} sc={score}')
            else:
                entry_ref = limit_price
                logger.info(f'🚀 被动LIMIT {sym} {side} @{limit_price} tier={tier} sc={score}')

            # 仓位乘数（连损减仓）
            pos_mult = get_position_multiplier()
            effective_margin = SCALP_MARGIN * effective_risk * pos_mult
            if pos_mult < 1.0:
                logger.info(f'  ⚠️ 连损减仓: 仓位×{pos_mult} (保证金{effective_margin:.1f}U)')

            side_map = {'LONG': 'BUY', 'SHORT': 'SELL'}

            # 开仓前强校验：再次确认未超限
            total_now = _fetch_total_positions()
            if total_now > MAX_TOTAL_POSITIONS:
                logger.warning(f'⏸️ 开仓前校验: 持仓{total_now}>{MAX_TOTAL_POSITIONS}，取消')
                continue

            # ─── 结构止损 + min/max约束 ───
            planned_sl = best.get('sl_price')
            planned_tp = best.get('tp_price')
            stop_min = best.get('stop_min', 0.35)
            stop_max = best.get('stop_max', 0.90)
            if planned_sl and planned_tp:
                risk_pct_plan = abs(planned_sl - entry_ref) / entry_ref * 100
                reward_pct_plan = abs(planned_tp - entry_ref) / entry_ref * 100
                # 结构止损太近 → 拉宽到 stop_min
                if risk_pct_plan < stop_min:
                    adj_factor = stop_min / risk_pct_plan
                    if side == 'LONG':
                        planned_sl = entry_ref * (1 - stop_min / 100)
                        planned_tp = entry_ref * (1 + reward_pct_plan / 100 * adj_factor)
                    else:
                        planned_sl = entry_ref * (1 + stop_min / 100)
                        planned_tp = entry_ref * (1 - reward_pct_plan / 100 * adj_factor)
                    from trading_bot.exchange.client import _align_sltp
                    planned_sl, planned_tp = _align_sltp(sym, planned_sl, planned_tp, side)
                    risk_pct_plan = stop_min
                    reward_pct_plan = reward_pct_plan * adj_factor
                    logger.info(f'   ⚠️ 结构止损太近，拉宽到{stop_min:.2f}%')
                # 结构止损太远 → 拒绝交易
                if risk_pct_plan > stop_max:
                    logger.info(f'   ❌ 结构止损过远({risk_pct_plan:.2f}% > {stop_max:.2f}%)，放弃交易')
                    return
            else:
                fallback_sl, fallback_tp = _calc_sltp(entry_ref, side, sym)
                risk_pct_plan = abs(fallback_sl - entry_ref) / entry_ref * 100
                reward_pct_plan = abs(fallback_tp - entry_ref) / entry_ref * 100
                planned_sl, planned_tp = fallback_sl, fallback_tp

            # ─── 仓位计算：止损越远仓位越小 ───
            equity = _fetch_account_equity()
            regime_probs = btc_env.get('regime_probs', {})
            confidence_factor = get_position_confidence_factor(regime_probs)
            risk_per_trade = min(0.50, equity * 0.002) * confidence_factor  # 信心度降仓
            max_notional = risk_per_trade / (risk_pct_plan / 100)
            cap_notional = SCALP_MARGIN * SCALP_LEVERAGE  # 固定保证金上限
            position_notional = min(max_notional, cap_notional)
            qty_val = position_notional / entry_ref
            aligned_qty = round(int(qty_val / step) * step, qty_decimals)

            # 确保名义价值 >= 5 USDT
            min_notional = 5.0
            if position_notional < min_notional:
                position_notional = min_notional
                qty_val = position_notional / entry_ref
                aligned_qty = round(int(qty_val / step) * step, qty_decimals)

            # 数量归零保护：高价币至少1个最小单位
            if aligned_qty <= 0:
                aligned_qty = step
                position_notional = aligned_qty * entry_ref
                logger.info(f'   📈 数量归零，强制1手 qty={aligned_qty} notional={position_notional:.0f}U')
            # 重新确保名义价值 >= 5 USDT（数量归零可能把notional又缩小了）
            if position_notional < 5.0:
                position_notional = 5.0
                qty_val = position_notional / entry_ref
                aligned_qty = round(int(qty_val / step) * step, qty_decimals)
                if aligned_qty <= 0:
                    aligned_qty = step
                    position_notional = aligned_qty * entry_ref
                logger.info(f'   📈 数量归零后notional仍不足5U，抬升到 {position_notional:.0f}U')
            effective_margin = position_notional / SCALP_LEVERAGE

            if effective_risk < 1.0:
                logger.info(f'   📉 风险系数{effective_risk:.2f}，止损{risk_pct_plan:.2f}%，仓位{position_notional:.0f}U')
            qty_str = ('%g' % aligned_qty).replace(',', '')

            if use_market:
                order = _api('POST', 'order', {
                    'symbol': sym, 'side': side_map[side], 'type': 'MARKET',
                    'quantity': qty_str, 'positionSide': side,
                })
                order_id = int(order.get('orderId', 0) or 0)
                # 以交易所真实持仓为准，不使用计划价或 fills[0]。
                time.sleep(0.5)
                pos = _query_live_position(sym, side)
                actual_qty = abs(float(pos.get('positionAmt', 0) or 0))
                actual_price = float(pos.get('entryPrice', 0) or 0)
                if actual_qty <= 0 or actual_price <= 0:
                    raise RuntimeError(f'市价入场未确认真实持仓 orderId={order_id}')
                aligned_limit = actual_price
                # 计算实际止损/止盈价格
                if side == 'LONG':
                    raw_sl = actual_price * (1 - risk_pct_plan / 100)
                    raw_tp = actual_price * (1 + reward_pct_plan / 100)
                else:
                    raw_sl = actual_price * (1 + risk_pct_plan / 100)
                    raw_tp = actual_price * (1 - reward_pct_plan / 100)

                # 创建交易所硬止损（断网/崩溃保护）
                sl_price, tp_price = round(raw_sl, 8), round(raw_tp, 8)
                try:
                    from trading_bot.exchange.protection import ensure_position_protection
                    prot_result = ensure_position_protection(
                        symbol=sym, position_side=side, actual_qty=actual_qty,
                        stop_price=sl_price, take_profit_price=tp_price,
                        mark_price=actual_price, owner_tag=str(order_id),
                    )
                    if not prot_result.stop_ok:
                        logger.critical(f'🚨 {sym} 止损创建失败，紧急平仓！')
                        from trading_bot.services.position_manager import market_close_position
                        market_close_position(sym, side, actual_qty)
                        raise RuntimeError(f'PROTECTION_FAILED: {sym}')
                    logger.info(f'✅ 市价入场: ID {order_id} @ {actual_price} qty={actual_qty} SL={sl_price}(hard) TP={tp_price}')
                except RuntimeError:
                    raise
                except Exception as prot_err:
                    logger.critical(f'🚨 {sym} 保护单创建异常: {prot_err}，紧急平仓')
                    try:
                        from trading_bot.services.position_manager import market_close_position
                        market_close_position(sym, side, actual_qty)
                    except Exception:
                        logger.critical(f'🚨 {sym} 紧急平仓也失败了！需人工处理')
                    raise RuntimeError(f'PROTECTION_EXCEPTION: {sym}')
                is_active = 'active'
                action_type = 'OPEN'
                label = '市价入场'
            else:
                aligned_limit = round(int(limit_price / tick) * tick, price_decimals)
                order = _api('POST', 'order', {
                    'symbol': sym, 'side': side_map[side], 'type': 'LIMIT',
                    'timeInForce': 'GTC', 'price': str(aligned_limit),
                    'quantity': qty_str, 'positionSide': side,
                })
                order_id = int(order.get('orderId', 0) or 0)
                if not order_id:
                    raise RuntimeError('LIMIT入场未返回orderId')
                # 未成交阶段不创建全额保护单，由 _sync_pending_orders 在真实成交后处理。
                sl_price = aligned_limit * (1 - risk_pct_plan / 100) if side == 'LONG' else aligned_limit * (1 + risk_pct_plan / 100)
                tp_price = aligned_limit * (1 + reward_pct_plan / 100) if side == 'LONG' else aligned_limit * (1 - reward_pct_plan / 100)
                logger.info(f'✅ LIMIT挂单已创建，等待真实成交后建立保护: ID {order_id} @ {aligned_limit}')
                is_active = 'pending'
                action_type = 'OPEN_LIMIT'
                label = 'LIMIT挂单'
            # 记录状态
            entry = {
                'symbol': sym,
                'side': side,
                'amount': effective_margin,
                'entry_price': aligned_limit,
                'strategy': 'scalp',
                'trade_type': best.get('trade_type', 'TREND_PULLBACK'),
                'status': is_active,
                'opened_at': datetime.now().isoformat(),
                'reason': best['reason'],
                'score': best['score'],
                'entry_type': label,
                'entry_order_id': order_id,
                'risk_pct': risk_pct_plan,
                'reward_pct': reward_pct_plan,
                'sl_price': sl_price,
                'tp_price': tp_price,
                # 分段止盈+移动止损
                'original_qty': actual_qty if use_market else 0,
                'qty': actual_qty if use_market else 0,
                'tp1_hit': False,
                'tp2_hit': False,
                'trailing_active': False,
                'highest_price': aligned_limit,
            }
            state = load_bot_state()
            pkey = position_key(sym, side)
            state.setdefault('positions', {})[pkey] = entry
            state.setdefault('trades', []).append({
                'action': action_type,
                'symbol': sym,
                'side': side,
                'amount': effective_margin,
                'entry_price': aligned_limit,
                'strategy': 'scalp',
                'reason': best['reason'],
                'time': datetime.now().isoformat(),
            })
            save_bot_state(state)
            logger.info(f'✅ 超短线{label}: {sym} {side} @ {aligned_limit}')

            # 通知（独立 try，失败不影响已保存的状态）
            try:
                from trading_bot.integrations.notifications import notify_entry
                notify_entry(sym, side, aligned_limit, actual_qty if use_market else 0,
                             sl_price, tp_price, best['score'], best['reason'])
            except Exception as note_err:
                logger.warning(f'  ⚠️ 通知发送失败（持仓已保存）: {note_err}')

        except Exception as e:
            logger.error(f'❌ 超短线LIMIT挂单失败: {e}')
            import traceback
            logger.error(traceback.format_exc())

    # ── 开仓状态通知：到达此处=未触发任何停止条件，开仓允许 ──
    _btc = btc_env if 'btc_env' in locals() else {}
    _check_trading_status(True, [], _btc)

    if __name__ == '__main__':
        run_scalper()

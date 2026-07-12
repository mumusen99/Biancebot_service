"""持仓审查模块 — 从 scalper.py 拆分，统一通过 gateway 操作。"""

import logging
import time
import pandas as pd
from datetime import datetime

from trading_bot.core.settings import BOT_STATE_FILE
from trading_bot.data.ws_market_client import market_cache
from trading_bot.exchange.market_data import fetch_klines
from trading_bot.exchange.protection import repair_existing_protection
from trading_bot.exchange.gateway import get_gateway
from trading_bot.strategy.indicators import compute_scalp_indicators as compute_indicators
from trading_bot.storage.state_store import save_state_atomic, load_state_safe

logger = logging.getLogger("position_review")
_gw = get_gateway()

# 兼容 scalper.py 的无参调用约定
def load_bot_state() -> dict:
    return load_state_safe(BOT_STATE_FILE)

def save_bot_state(state: dict) -> None:
    save_state_atomic(BOT_STATE_FILE, state)

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


def _fix_existing_sltp():
    """校验并修复现有 scalp 持仓的保护单（使用真实qty，不重新计算目标价）。"""
    from trading_bot.exchange.client import _load_precisions
    _load_precisions()

    state = load_bot_state()
    scalp = {k: v for k, v in state.get('positions', {}).items()
             if v.get('strategy') == 'scalp' and v.get('status') != 'pending'}
    if not scalp:
        return

    logger.info(f'  🔍 检查已有 scalp 止盈止损 ({len(scalp)}个)')

    for pkey, info in scalp.items():
        sym = info.get('symbol', '')
        side = info.get('side', 'LONG')
        if not sym:
            continue

        try:
            pos = _query_live_position(sym, side)
            actual_qty = abs(float(pos.get('positionAmt', 0) or 0))
            if actual_qty <= 0:
                continue

            # 使用已有止损价，不重新计算
            sl = float(info.get('sl_price', 0) or 0)
            tp = float(info.get('tp_price', 0) or 0)
            if not sl:
                continue

            mark = float(pos.get('markPrice', 0) or 0)
            result = repair_existing_protection(info, pos)
            if result.stop_ok:
                logger.info(f'    ✅ 保护单确认 {sym} SL={sl:.4f} TP={tp:.4f}')
            else:
                logger.warning(f'    ⚠️ 保护单修复失败 {sym}: {result.reason}')
        except Exception as exc:
            logger.warning(f'    ⚠️ 保护检查失败 {sym}: {exc}')

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

def _sync_pending_orders():
    """检测 LIMIT 入场成交；发现任何成交后先撤余单，再按真实仓位建立保护。"""
    state = load_bot_state()
    pending = {k: v for k, v in state.get('positions', {}).items()
               if v.get('strategy') == 'scalp' and v.get('status') == 'pending'}
    if not pending:
        return

    changed = False
    for pkey, info in pending.items():
        sym = info.get('symbol', pkey.split(':')[0])
        side = info.get('side', '')
        order_id = info.get('entry_order_id')
        try:
            pos = _query_live_position(sym, side)
            filled_qty = abs(float(pos.get('positionAmt', 0) or 0))
            if filled_qty <= 0:
                # 检查挂单是否过期 (90s)
                opened = info.get('time') or info.get('opened_at', '')
                if opened:
                    try:
                        from datetime import datetime as dt
                        age = (dt.now() - dt.fromisoformat(str(opened))).total_seconds()
                        if age > 90:
                            logger.info(f'  ⏰ 限价单过期{age:.0f}s，取消: {sym}')
                            if order_id:
                                _cancel_limit_order(sym, int(order_id))
                            state['positions'][pkey]['status'] = 'closed'
                            changed = True
                            continue
                    except Exception:
                        logger.debug(f'  ⏰ {sym} 无法解析opened_at，跳过过期检查')
                logger.info(f'  ⏳ 限价单未成交: {sym} orderId={order_id}')
                continue

            # 一旦出现成交，取消剩余挂单，避免保护数量建立后又继续增仓。
            if order_id and not _cancel_entry_safely(sym, int(order_id)):
                state['positions'][pkey]['status'] = 'ENTRY_CANCEL_PENDING'
                state['positions'][pkey]['last_error'] = 'ENTRY_REMAINDER_CANCEL_FAILED'
                changed = True
                logger.critical(f'  🚨 {sym} 已部分成交但剩余入场单取消失败，暂停自动处理')
                continue

            # 取消后重新读取最终真实数量和均价。
            time.sleep(0.5)
            pos = _query_live_position(sym, side)
            filled_qty = abs(float(pos.get('positionAmt', 0) or 0))
            filled_entry = float(pos.get('entryPrice', 0) or 0)
            if filled_qty <= 0 or filled_entry <= 0:
                state['positions'][pkey]['status'] = 'UNKNOWN'
                changed = True
                continue

            risk_pct = float(info.get('risk_pct', SL_PRICE_PCT))
            reward_pct = float(info.get('reward_pct', TP_PRICE_PCT))
            # WS 本地监控，不挂条件单
            if side == 'LONG':
                sl_price = round(filled_entry * (1 - risk_pct / 100), 8)
                tp_price = round(filled_entry * (1 + reward_pct / 100), 8)
            else:
                sl_price = round(filled_entry * (1 + risk_pct / 100), 8)
                tp_price = round(filled_entry * (1 - reward_pct / 100), 8)

            state['positions'][pkey].update({
                'status': 'active', 'entry_price': filled_entry,
                'filled_qty': filled_qty, 'sl_price': sl_price,
                'tp_price': tp_price,
                'original_qty': filled_qty, 'qty': filled_qty,
                'tp1_hit': False, 'tp2_hit': False,
                'trailing_active': False, 'highest_price': filled_entry,
            })
            state.setdefault('trades', []).append({
                'action': 'FILLED', 'symbol': sym, 'side': side,
                'entry_price': filled_entry, 'filled_qty': filled_qty,
                'time': datetime.now().isoformat(),
            })
            changed = True
            logger.info(f'  ✅ LIMIT成交并完成保护: {sym} entry={filled_entry} qty={filled_qty}')
        except Exception as e:
            logger.error(f'  ❌ pending订单同步失败 {sym}: {e}')

    if changed:
        save_bot_state(state)


# ═══════════════════════════════════════════════════
#  执行入口
# ═══════════════════════════════════════════════════

def _review_scalp_positions():
    """
    检查现有 scalp 持仓和挂单是否仍然有效。
    如果趋势反了→主动平仓、取消限价单。
    """
    import requests as rq
    from trading_bot.core.settings import PROXY, API_KEY, API_SECRET as SECRET
    from trading_bot.exchange.client import _api

    state = load_bot_state()
    scalp = {k: v for k, v in state.get('positions', {}).items()
             if v.get('strategy') == 'scalp'}
    if not scalp:
        return

    # 获取交易所挂单（用于取消）、持仓（用于平仓）
    try:
        all_positions = _gw.get_positions()
        exchange_pos = {p.symbol: {
            'symbol': p.symbol, 'positionAmt': str(p.position_amt),
            'positionSide': p.position_side.value, 'entryPrice': str(p.entry_price)
        } for p in all_positions}
        
        all_orders = _gw.get_open_orders()
        open_orders = [{'symbol': o.symbol, 'orderId': o.order_id,
                       'clientOrderId': o.client_order_id,
                       'positionSide': o.position_side.value}
                      for o in all_orders]
    except Exception as e:
        logger.warning(f'  ⚠️ 获取交易所数据失败: {e}')
        return

    changed = False
    for pkey, info in scalp.items():
        sym = info.get('symbol', pkey.split(':')[0])
        side = info.get('side', '')
        status = info.get('status', 'active')
        entry_price = info.get('entry_price', 0)

        try:
            df = _fetch_klines_ws(sym, TIMEFRAME, 30)
            if df is None or df.empty:
                continue
            df = compute_indicators(df)
            # ─── 只用已收盘K线 (iloc[-2]) 判断趋势，避免未收盘K线flicker ───
            last_closed = df.iloc[-2]
            prev_closed = df.iloc[-3] if len(df) >= 3 else last_closed
            close_c = float(last_closed['close'])
            ema9_c = float(last_closed.get('ema9', close_c))
            ema21_c = float(last_closed.get('ema21', close_c))
            rsi_c = float(last_closed.get('rsi', 50))
            rsi_prev = float(prev_closed.get('rsi', rsi_c))
            vwap_c = float(last_closed.get('vwap', close_c))
            ema20_c = float(last_closed.get('ema20', close_c))
            vol_c = float(last_closed.get('volume', 0))
            vol_avg_c = float(last_closed.get('vol_avg', 1))
            trend_up = ema9_c > ema21_c
            trend_down = ema9_c < ema21_c

            # 当前未收盘价（仅用于实时价格/硬止损/浮盈计算）
            last = df.iloc[-1]
            cur_price = float(last['close'])

            # ─── 6条件软退出评分 ───
            # 只用已收盘K线，每项满足得1分，需≥2分才退出
            soft_exit_score = 0
            exit_reasons = []

            # 条件1: 连续两根已收盘K线跌破关键支撑(VWAP/EMA20)
            close_p2 = float(df.iloc[-3]['close']) if len(df) >= 3 else close_c
            if side == 'LONG':
                below_vwap = close_c < vwap_c and close_p2 < float(df.iloc[-3].get('vwap', vwap_c))
                below_ema20 = close_c < ema20_c and close_p2 < float(df.iloc[-3].get('ema20', ema20_c))
                if below_vwap or below_ema20:
                    soft_exit_score += 1
                    exit_reasons.append('连续跌破支撑')
            else:
                above_vwap = close_c > vwap_c and close_p2 > float(df.iloc[-3].get('vwap', vwap_c))
                above_ema20 = close_c > ema20_c and close_p2 > float(df.iloc[-3].get('ema20', ema20_c))
                if above_vwap or above_ema20:
                    soft_exit_score += 1
                    exit_reasons.append('连续突破阻力')

            # 条件2: Lower Low (LONG) / Higher High (SHORT)
            low_c = float(last_closed.get('low', close_c))
            low_p = float(prev_closed.get('low', low_c))
            high_c = float(last_closed.get('high', close_c))
            high_p = float(prev_closed.get('high', high_c))
            if side == 'LONG' and low_c < low_p:
                soft_exit_score += 1
                exit_reasons.append('LowerLow')
            elif side == 'SHORT' and high_c > high_p:
                soft_exit_score += 1
                exit_reasons.append('HigherHigh')

            # 条件3: EMA9下穿EMA20（LONG）/ EMA9上穿EMA20（SHORT）
            ema9_p = float(prev_closed.get('ema9', ema9_c))
            ema20_p = float(prev_closed.get('ema20', ema20_c))
            if side == 'LONG' and ema9_c < ema20_c and ema9_p >= ema20_p:
                soft_exit_score += 1
                exit_reasons.append('EMA9穿EMA20')
            elif side == 'SHORT' and ema9_c > ema20_c and ema9_p <= ema20_p:
                soft_exit_score += 1
                exit_reasons.append('EMA9穿EMA20')

            # 条件4: 回撤放量
            if vol_avg_c > 0 and vol_c > vol_avg_c * 1.3:
                soft_exit_score += 1
                exit_reasons.append('放量')

            # 条件5: RSI < 38 且仍在下降 (LONG) / RSI > 62 且仍在上升 (SHORT)
            if side == 'LONG' and rsi_c < 38 and rsi_c < rsi_prev:
                soft_exit_score += 1
                exit_reasons.append(f'RSI{rsi_c:.0f}↓')
            elif side == 'SHORT' and rsi_c > 62 and rsi_c > rsi_prev:
                soft_exit_score += 1
                exit_reasons.append(f'RSI{rsi_c:.0f}↑')

            # 条件6: 趋势反转 (EMA9方向改变)
            ema9_pp = float(df.iloc[-4].get('ema9', ema9_c)) if len(df) >= 4 else ema9_p
            trend_was_up = ema9_p > ema20_p
            trend_was_down = ema9_p < ema20_p
            if side == 'LONG' and trend_was_up and trend_down:
                soft_exit_score += 1
                exit_reasons.append('趋势转空')
            elif side == 'SHORT' and trend_was_down and trend_up:
                soft_exit_score += 1
                exit_reasons.append('趋势转多')

            # ─── 计算持仓时间 ───
            hold_seconds = 0
            opened_str = info.get('opened_at', '')
            if opened_str:
                try:
                    opened_dt = datetime.fromisoformat(opened_str)
                    hold_seconds = (datetime.now() - opened_dt).total_seconds()
                except:
                    pass

            # ─── 生命周期退出决策 ───
            # Phase 0: 保护期 0-60s — 仅硬止损，禁止所有软件退出
            # Phase 1: 启动期 60-180s — 需形成初步进展
            # Phase 2: 观察期 180-480s — 软退出 + 时间退出
            # Phase 3: 收割期 480-1500s — 严格时间退出
            # Phase 4: 硬上限 >1500s — 强制退出

            phase = 'protect'
            if hold_seconds >= 1500: phase = 'hard_cap'
            elif hold_seconds >= 480: phase = 'harvest'
            elif hold_seconds >= 180: phase = 'observe'
            elif hold_seconds >= 60: phase = 'startup'

            if phase == 'protect':
                # 不执行任何软件退出，只更新最佳浮盈
                if entry_price:
                    pnl = (cur_price-entry_price)/entry_price*100 if side=='LONG' else (entry_price-cur_price)/entry_price*100
                    if pnl > info.get('best_pnl_pct', -999):
                        info['best_pnl_pct'] = round(pnl, 4)
                        changed = True
                continue

            # 计算当前浮盈
            current_pnl_pct = 0
            if entry_price:
                current_pnl_pct = (cur_price-entry_price)/entry_price*100 if side=='LONG' else (entry_price-cur_price)/entry_price*100
            best_pnl = info.get('best_pnl_pct', -999)
            if current_pnl_pct > best_pnl:
                info['best_pnl_pct'] = round(current_pnl_pct, 4)
                changed = True
            best_pnl = max(best_pnl, current_pnl_pct)

            exit_trigger = False
            exit_label = ''

            if phase == 'hard_cap':
                exit_trigger = True
                exit_label = f'HARD_CAP_EXIT hold={hold_seconds:.0f}s'

            elif phase == 'harvest':
                # 8-25min: 未达0.5R且动能衰退 → 退出；已达0.8R则追踪
                r_multiple = best_pnl / info.get('risk_pct', 0.5) if info.get('risk_pct') else 0
                if r_multiple < 0.5 and best_pnl < 0.15:
                    exit_trigger = True
                    exit_label = f'HARVEST_EXIT r={r_multiple:.1f} mfe={best_pnl:.2f}%'
                elif soft_exit_score >= 2:
                    exit_trigger = True
                    exit_label = f'HARVEST_SOFT_EXIT({"+".join(exit_reasons)})'

            elif phase == 'observe':
                # 3-8min: 软退出≥2分 或 时间退出(6min mfe<0.15, 4min mfe<0.10)
                soft_exit = soft_exit_score >= 2
                time_exit_6m = hold_seconds >= 360 and best_pnl < 0.15
                time_exit_4m = hold_seconds >= 240 and best_pnl < 0.10
                if soft_exit:
                    exit_trigger = True
                    exit_label = f'SOFT_EXIT({"+".join(exit_reasons)})'
                elif time_exit_6m:
                    exit_trigger = True
                    exit_label = f'TIME_EXIT_6MIN mfe={best_pnl:.2f}%'
                elif time_exit_4m:
                    exit_trigger = True
                    exit_label = f'TIME_EXIT_4MIN mfe={best_pnl:.2f}%'

            elif phase == 'startup':
                # 1-3min: 需初始进展，无进展+结构弱化 → 退出
                if soft_exit_score >= 3:
                    exit_trigger = True
                    exit_label = f'STARTUP_FAIL({"+".join(exit_reasons)})'
                elif best_pnl < 0.02 and soft_exit_score >= 1:
                    exit_trigger = True
                    exit_label = f'STARTUP_STALL mfe={best_pnl:.2f}%'

            if not exit_trigger:
                # ─── SL管理 ───
                if status == 'active' and entry_price and info.get('risk_pct') and phase not in ('protect',):
                    try:
                        risk_pct = float(info['risk_pct'])
                        if side == 'LONG':
                            profit_pct = current_pnl_pct
                            new_sl = None
                            if profit_pct >= 1.0 * risk_pct:
                                new_sl = entry_price * (1 + 0.5 * risk_pct / 100)
                            elif profit_pct >= 0.6 * risk_pct:
                                new_sl = entry_price
                            if new_sl and new_sl > float(info.get('sl_price', 0)):
                                info['sl_price'] = round(new_sl, 8)
                                changed = True
                                logger.info(f'  📈 {sym} {phase} 浮盈{profit_pct:.2f}% → SL移到{new_sl:.4f}')
                        else:
                            profit_pct = current_pnl_pct
                            new_sl = None
                            if profit_pct >= 1.0 * risk_pct:
                                new_sl = entry_price * (1 - 0.5 * risk_pct / 100)
                            elif profit_pct >= 0.6 * risk_pct:
                                new_sl = entry_price
                            if new_sl and new_sl < float(info.get('sl_price', 9e9)):
                                info['sl_price'] = round(new_sl, 8)
                                changed = True
                                logger.info(f'  📈 {sym} {phase} 浮盈{profit_pct:.2f}% → SL移到{new_sl:.4f}')
                    except Exception:
                        pass
                continue

            # ─── 执行平仓/取消 ───
            action = '平仓' if status == 'active' else '取消挂单'
            logger.info(f'  ⚠️ {exit_label} {action}: {sym} {side} entry={entry_price} cur={cur_price:.4f} hold={hold_seconds:.0f}s phase={phase}')
            
            if status == 'pending':
                # 取消交易所限价单
                for o in open_orders:
                    if o['symbol'] == sym and o['positionSide'] == side:
                        try:
                            _api('DELETE', 'order', {'symbol': sym, 'orderId': o['orderId']})
                            logger.info(f'    ✅ 取消限价单: {sym} ID:{o["orderId"]}')
                        except Exception as e:
                            logger.warning(f'    ⚠️ 取消失败: {e}')
                
                # 取消条件委托（止盈止损）
                algos = _get_algo_orders(sym)
                for a in algos:
                    if a.get('algoStatus') in ('NEW', 'WORKING'):
                        try:
                            _cancel_algo(sym, a["algoId"])
                        except:
                            pass
            
            else:  # active → 市价平仓
                # 先取消止盈止损条件单
                algos = _get_algo_orders(sym)
                for a in algos:
                    if a.get('algoStatus') in ('NEW', 'WORKING'):
                        try:
                            _cancel_algo(sym, a["algoId"])
                        except:
                            pass
                
                # 市价平仓（反方向）
                pos = exchange_pos.get(sym)
                if pos:
                    qty = abs(float(pos['positionAmt']))
                    close_side = 'SELL' if side == 'LONG' else 'BUY'
                    try:
                        from trading_bot.exchange.client import _get_symbol_precision, _load_precisions
                        _load_precisions()
                        _, step, _, _ = _get_symbol_precision(sym)
                        qty_aligned = round(int(qty / step) * step, 8)
                        qty_str = ('%g' % qty_aligned).replace(',', '')
                        
                        _api('POST', 'order', {
                            'symbol': sym, 'side': close_side,
                            'type': 'MARKET',
                            'quantity': qty_str,
                            'positionSide': side,
                        })
                        logger.info(f'    ✅ 市价平仓: {sym} {side} {qty}张')
                    except Exception as e:
                        logger.warning(f'    ⚠️ 平仓失败: {e}')
            
            # 从 bot_state 删除
            if pkey in state.get('positions', {}):
                close_pnl = info.get('pnl', 0) or 0
                state['closed_pnl'] = round(state.get('closed_pnl', 0) + close_pnl, 2)
                state.setdefault('trades', []).append({
                    'action': 'CLOSE',
                    'symbol': sym, 'side': side, 'strategy': 'scalp',
                    'pnl': round(close_pnl, 2),
                    'reason': f'{exit_label} phase={phase}',
                    'time': datetime.now().isoformat(),
                    'hold_seconds': round(hold_seconds, 1),
                })
                del state['positions'][pkey]
                changed = True
                logger.info(f'    ✅ 从bot_state删除: {sym}')
        except Exception as e:
            logger.debug(f'  检查 {sym} 趋势失败: {e}')
    
    if changed:
        save_bot_state(state)
        logger.info('  ✅ 趋势反查完成，已处理')


def _cleanup_stale_algos():
    """清理多余/孤立条件委托
    1. 每个持仓只保留最新 1止损 + 1止盈（去重）
    2. 清除 bot_state 中已不存在的币种的条件单（孤立条件单）
    """
    state = load_bot_state()
    scalp_syms = set(k for k, v in state.get('positions', {}).items()
                     if v.get('strategy') == 'scalp')
    
    # 从历史交易记录里提取所有曾出现过的 scalp 币种
    for t in state.get('trades', []):
        if t.get('strategy') == 'scalp' and t.get('action') in ('OPEN_LIMIT', 'OPEN', 'FILLED'):
            sym = t.get('symbol', '')
            if sym and len(sym) > 3:
                scalp_syms.add(sym)
    
    if not scalp_syms:
        return
    
    # 逐个币查，收集所有有效条件委托
    from collections import defaultdict
    by_sym = defaultdict(list)
    for sym in sorted(scalp_syms):
        try:
            algos = _get_algo_orders(sym)
            for a in algos:
                if a.get('algoStatus') in ('NEW', 'WORKING'):
                    by_sym[sym].append(a)
        except:
            pass
    
    for sym, active in by_sym.items():
        in_bot = sym in set(k for k, v in state.get('positions', {}).items()
                           if v.get('strategy') == 'scalp')
        
        if not in_bot:
            # 币不在 bot_state 中 → 全部取消（孤立条件单）
            logger.info(f'  清理孤立条件单: {sym} ({len(active)}个)')
            for a in active:
                try:
                    _cancel_algo(sym, a["algoId"])
                    logger.info(f'    取消: {a["orderType"]} tp={a.get("triggerPrice")}')
                except:
                    pass
                time.sleep(0.2)
        else:
            # 在 bot_state 中 → 去重，只保留最新各1个
            stops = [a for a in active if 'STOP' in a.get('orderType', '').upper()]
            profits = [a for a in active if 'PROFIT' in a.get('orderType', '').upper()]
            to_cancel = []
            for group, name in [(stops, '止损'), (profits, '止盈')]:
                if len(group) > 1:
                    group.sort(key=lambda x: x.get('algoId', 0))
                    for a in group[:-1]:
                        to_cancel.append(a)
                        logger.info(f'  清理多余{name}: {sym} tp={a.get("triggerPrice")}')
            for a in to_cancel:
                try:
                    _cancel_algo(sym, a["algoId"])
                except:
                    pass
                time.sleep(0.2)


def _sync_orphan_positions():
    """启动时将交易所持仓同步到 bot 状态（修复孤儿仓）。"""
    from trading_bot.exchange.gateway import get_gateway
    gw = get_gateway()

    state = load_bot_state()
    tracked = set()
    for k, v in state.get('positions', {}).items():
        if v.get('strategy') == 'scalp':
            tracked.add(v.get('symbol', ''))

    try:
        exchange_positions = gw.get_positions()
        orphan_count = 0
        for p in exchange_positions:
            if abs(p.position_amt) <= 0:
                continue
            sym = p.symbol
            if sym in tracked:
                continue

            side = 'LONG' if p.position_amt > 0 else 'SHORT'
            entry_price = float(p.entry_price)
            qty = abs(float(p.position_amt))
            pkey = f'{sym}:{side}'

            # 默认 0.5% 止损 / 1.0% 止盈
            if side == 'LONG':
                sl_price = round(entry_price * 0.995, 8)
                tp_price = round(entry_price * 1.010, 8)
            else:
                sl_price = round(entry_price * 1.005, 8)
                tp_price = round(entry_price * 0.990, 8)

            entry = {
                'symbol': sym, 'side': side,
                'amount': qty * entry_price / 3,
                'entry_price': entry_price,
                'strategy': 'scalp', 'status': 'active',
                'opened_at': datetime.now().isoformat(),
                'reason': '孤儿仓恢复', 'score': 0,
                'entry_type': '孤儿仓同步',
                'risk_pct': 0.5, 'reward_pct': 1.0,
                'sl_price': sl_price, 'tp_price': tp_price,
                'original_qty': qty, 'qty': qty,
                'tp1_hit': False, 'tp2_hit': False,
                'trailing_active': False,
                'highest_price': entry_price,
            }
            state.setdefault('positions', {})[pkey] = entry
            orphan_count += 1
            logger.warning(f'  🩹 孤儿仓恢复: {sym} {side} @ {entry_price} SL={sl_price} TP={tp_price}')

        if orphan_count > 0:
            save_state_atomic(BOT_STATE_FILE, state)
            logger.info(f'🩹 已恢复 {orphan_count} 个孤儿仓到 bot 状态')
    except Exception as e:
        logger.warning(f'  ⚠️ 孤儿仓扫描失败: {e}')



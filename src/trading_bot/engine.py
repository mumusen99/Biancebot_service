# ⚠️ FROZEN: legacy module. Only fatal security fixes allowed. No new features.
from __future__ import annotations

import logging
import os
import signal
import time
import threading

from trading_bot.core.runtime_config import get_runtime_config
from trading_bot.core.env_config import get_exchange_config, print_startup_info
from trading_bot.services.connectivity import ensure_connection
from trading_bot.services.position_manager import run_full_cycle
from trading_bot.strategy import scalper
from trading_bot.data.ws_market_client import ws_client, market_cache
from trading_bot.strategy.candidate_pool import candidate_pool
from trading_bot.risk.rate_limiter import traffic_monitor
from trading_bot.execution.execution_priority_queue import execution_queue, latency_tracker
from trading_bot.strategy.incremental_features import feature_store

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [engine] %(levelname)s %(message)s",
)
logger = logging.getLogger("trading_bot.engine")
_STOP = False


def _stop(*_args):
    global _STOP
    _STOP = True


def _start_ws():
    """启动 WebSocket 客户端，订阅全市场 ticker + kline"""
    try:
        from trading_bot.strategy.market_regime import scan_top_coins
        cfg = get_runtime_config()
        universe = int(cfg.get("engine", {}).get("universe_size", 100))
        top_coins, _ = scan_top_coins(min_volume_usdt=500000, max_coins=universe, top_n=universe)
        universe_symbols = [c['symbol'] for c in top_coins[:universe]]
        universe_symbols.append('BTCUSDT')
        universe_symbols = list(dict.fromkeys(universe_symbols))

        if universe_symbols:
            ws_client.subscribe_tickers(universe_symbols)
            # klines走REST，不占WS流
            ws_client.start()
            logger.info(f"WS started: {len(universe_symbols)} tickers")
            return universe_symbols
    except Exception as e:
        logger.warning(f"WS startup failed, using REST fallback: {e}")
    return []


def run() -> None:
    # 环境验证：TRADING_ENV + LIVE_TRADING_ACK 在 get_exchange_config() 中完成
    cfg = get_exchange_config()
    print_startup_info(cfg)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    next_manage = 0.0
    next_scan = 0.0
    candidate_symbols = []

    logger.info("engine starting (WS+REST hybrid mode)")

    # 异步启动 WS（不阻塞主循环）
    ws_thread = threading.Thread(target=_start_ws, daemon=True)
    ws_thread.start()

    loop_count = 0
    while not _STOP:
        loop_count += 1
        runtime_cfg = get_runtime_config()
        now_s = time.monotonic()

        # ── 处理执行队列（最高优先级，每次循环都处理）──
        execution_queue.process_all(max_count=5)

        # ── 分段止盈 + 移动止损（每次循环，WS实时价格）──
        try:
            from trading_bot.services.position_manager import load_bot_state, save_bot_state, market_close_position as _mcp
            state = load_bot_state()
            changed = False
            for key, pos in state.get('positions', {}).items():
                # 有entry_price就检查，不依赖status（state可能被覆盖成closed）
                sym = pos.get('symbol', '')
                side = pos.get('side', 'LONG')
                entry = float(pos.get('entry_price', 0))
                if entry <= 0:
                    continue
                bt = market_cache.get_book_ticker(sym)
                if not bt:
                    price = float(pos.get('current_price', 0) or pos.get('entry_price', 0))
                    if price <= 0:
                        continue
                else:
                    price = (bt['b'] + bt['a']) / 2
                # 初始化分段参数
                orig_qty = float(pos.get('original_qty') or pos.get('qty', 0))
                cur_qty = float(pos.get('qty', orig_qty))
                # 检查所有交易所持仓（不依赖status字段）
                sl = float(pos.get('sl_price', 0) or 0)
                if loop_count % 60 == 0:
                    logger.info(f'[监控] {sym} price={price:.5f} sl={sl:.5f} qty={cur_qty}')
                if sl <= 0:
                    sym_entry = float(pos.get('entry_price', 0))
                    if sym_entry <= 0:
                        continue
                    if side == 'LONG':
                        sl = round(sym_entry * 0.995, 8)
                    else:
                        sl = round(sym_entry * 1.005, 8)
                    pos['sl_price'] = sl
                
                risk_dist = entry - sl if side == 'LONG' else sl - entry  # R
                if risk_dist <= 0:
                    continue
                
                tp1 = round(entry + 1.0 * risk_dist if side == 'LONG' else entry - 1.0 * risk_dist, 8)
                tp2 = round(entry + 1.5 * risk_dist if side == 'LONG' else entry - 1.5 * risk_dist, 8)
                tp3 = round(entry + 2.5 * risk_dist if side == 'LONG' else entry - 2.5 * risk_dist, 8)

                pos['tp1_price'] = tp1; pos['tp2_price'] = tp2; pos['tp3_price'] = tp3
                pos['original_qty'] = orig_qty
                
                # --- 止损（需连续2次确认防插针）---
                hit_sl = (side == 'LONG' and price <= sl) or (side == 'SHORT' and price >= sl)
                old_sl = int(pos.get('_sl_confirm', 0))
                sl_conf = old_sl + 1 if hit_sl else 0
                if sl_conf != old_sl:
                    pos['_sl_confirm'] = sl_conf; changed = True
                if sl_conf >= 2:
                    logger.warning(f'🛑 止损确认: {sym} price={price} sl={sl} (连续{sl_conf}次)')
                    if _mcp(sym, side, cur_qty):
                        try:
                            from trading_bot.integrations.notifications import notify_exit
                            from trading_bot.strategy.scalper import record_trade_result
                            pnl = (price - entry) * cur_qty if side == 'LONG' else (entry - price) * cur_qty
                            record_trade_result(pnl)
                            notify_exit(sym, side, price, pnl, f'止损@{sl}')
                        except Exception: pass
                        pos['status'] = 'closed'; changed = True; continue
                    else:
                        pos['_sl_confirm'] = 1  # 下次重试，不重复确认
                
                # --- TP1: 50%（连续2次确认）---
                if not pos.get('tp1_hit'):
                    hit = (side == 'LONG' and price >= tp1) or (side == 'SHORT' and price <= tp1)
                    old_c = int(pos.get('_tp1_confirm', 0))
                    c = old_c + 1 if hit else 0
                    if c != old_c:
                        pos['_tp1_confirm'] = c; changed = True
                    if c >= 2:
                        qty1 = max(1, int(orig_qty * 0.50))
                        logger.warning(f'🎯 TP1: {sym} 50%({qty1}) @ {price}')
                        if _mcp(sym, side, qty1):
                            pos['tp1_hit'] = True
                        pos['qty'] = cur_qty - qty1
                        pos['trailing_active'] = True
                        pos['highest_price'] = price
                        changed = True
                        cur_qty = pos['qty']
                        if cur_qty <= 0:
                            try:
                                from trading_bot.integrations.notifications import notify_exit
                                from trading_bot.strategy.scalper import record_trade_result
                                pnl = (price - entry) * orig_qty if side == 'LONG' else (entry - price) * orig_qty
                                record_trade_result(pnl)
                                notify_exit(sym, side, price, pnl, 'TP1全平')
                            except Exception: pass
                            pos['status'] = 'closed'; continue
                
                # --- TP2: 30%（连续2次确认）---
                if not pos.get('tp2_hit'):
                    hit = (side == 'LONG' and price >= tp2) or (side == 'SHORT' and price <= tp2)
                    old_c2 = int(pos.get('_tp2_confirm', 0))
                    c2 = old_c2 + 1 if hit else 0
                    if c2 != old_c2:
                        pos['_tp2_confirm'] = c2; changed = True
                    if c2 >= 2:
                        qty2 = max(1, int(orig_qty * 0.30))
                        logger.warning(f'🎯 TP2: {sym} 30%({qty2}) @ {price}')
                        if _mcp(sym, side, qty2):
                            pos['tp2_hit'] = True
                        pos['qty'] = max(0, cur_qty - qty2)
                        changed = True
                        cur_qty = pos['qty']
                        if cur_qty <= 0:
                            try:
                                from trading_bot.integrations.notifications import notify_exit
                                from trading_bot.strategy.scalper import record_trade_result
                                pnl = (price - entry) * orig_qty if side == 'LONG' else (entry - price) * orig_qty
                                record_trade_result(pnl)
                                notify_exit(sym, side, price, pnl, 'TP2全平')
                            except Exception: pass
                            pos['status'] = 'closed'; continue
                
                # --- Runner 移动止损（激活点=TP3@1.8R，回撤0.5R平仓）---
                trail_activate = tp3
                trail_dist = round(risk_dist * 0.5, 8)
                if pos.get('trailing_active') or (side == 'LONG' and price >= trail_activate) or (side == 'SHORT' and price <= trail_activate):
                    pos['trailing_active'] = True
                    highest = float(pos.get('highest_price', price))
                    if (side == 'LONG' and price > highest) or (side == 'SHORT' and price < highest):
                        pos['highest_price'] = price
                        highest = price
                        pos['_trail_confirm'] = 0; changed = True
                    hit_trail = (side == 'LONG' and price <= highest - trail_dist) or (side == 'SHORT' and price >= highest + trail_dist)
                    old_tc = int(pos.get('_trail_confirm', 0))
                    tc = old_tc + 1 if hit_trail else 0
                    if tc != old_tc:
                        pos['_trail_confirm'] = tc; changed = True
                    if tc >= 2:
                        runner_qty = float(pos.get('qty', cur_qty))
                        logger.warning(f'🏃 移动止损: {sym} 从最高{highest}回落至{price} (剩余{runner_qty}张)')
                        if _mcp(sym, side, runner_qty):
                            try:
                                from trading_bot.integrations.notifications import notify_exit
                                from trading_bot.strategy.scalper import record_trade_result
                                pnl = (price - entry) * runner_qty if side == 'LONG' else (entry - price) * runner_qty
                                record_trade_result(pnl)
                                notify_exit(sym, side, price, pnl, f'移动止损 最高{highest}')
                            except Exception: pass
                            pos['status'] = 'closed'; changed = True
                        else:
                            pos['_trail_confirm'] = 1
            
            if changed:
                save_bot_state(state)
        except Exception:
            pass

        # ── 持仓管理（间隔从 runtime 读取）──
        manage_interval = int(runtime_cfg.get("engine", {}).get("manage_interval_seconds", 10))
        if now_s >= next_manage:
            try:
                run_full_cycle()
            except Exception:
                logger.exception("position management cycle failed")
            next_manage = now_s + manage_interval

        # ── 策略扫描（间隔从 runtime 读取）──
        scan_interval = int(runtime_cfg.get("engine", {}).get("scan_interval_seconds", 30))
        if now_s >= next_scan:
            if runtime_cfg.get("strategy", {}).get("enabled", True):
                try:
                    if ensure_connection():
                        scalper.apply_runtime_config(runtime_cfg)
                        scalper.run_scalper()
                        # 候选池更新 + 旧候选退订
                        prev = set(candidate_symbols)
                        candidate_symbols = list(candidate_pool.symbols)
                        curr = set(candidate_symbols)
                        if curr != prev:
                            removed = prev - curr
                            if removed and len(candidate_symbols) >= 3:
                                ws_client.subscribe_candidate(candidate_symbols[:5])
                                logger.debug("candidate update: +%d -%d", len(curr - prev), len(removed))
                    else:
                        logger.error("Binance unavailable; scan skipped")
                except Exception:
                    logger.exception("strategy cycle failed")
            next_scan = now_s + scan_interval

        # ── 健康检查（每30s）──
        if int(now_s) % 30 == 0 and int(now_s) != getattr(run, '_last_health', 0):
            run._last_health = int(now_s)
            tickers = market_cache.active_ticker_count
            klines = market_cache.active_kline_count
            eq_stats = execution_queue.stats
            logger.info(f"health: tickers={tickers} klines={klines} stale={market_cache.stale_count} "
                       f"queue={eq_stats['queue_size']} inflight={eq_stats['in_flight']} "
                       f"exec={eq_stats['total_executed']} drop={eq_stats['total_dropped']}")
            # 数据新鲜度告警
            if market_cache.stale_count > 10:
                logger.warning("STALE DATA: %d symbols outdated", market_cache.stale_count)

        time.sleep(0.5)

    ws_client.stop()
    logger.info("engine stopped")

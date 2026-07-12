# ⚠️ FROZEN: legacy module. Only fatal security fixes allowed. No new features.
from __future__ import annotations

import logging
import logging.handlers
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
from trading_bot.execution.position_supervisor import PositionSupervisor
from trading_bot.strategy.incremental_features import feature_store

import os
LOG_DIR = os.getenv("TRADING_LOG_DIR", "/opt/trading-bot/current/logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 添加文件handler（basicConfig已被scalper先调用，给engine logger单独加）
os.makedirs(LOG_DIR, exist_ok=True)
_fh = logging.handlers.RotatingFileHandler(
    f"{LOG_DIR}/engine.log", maxBytes=5*1024*1024, backupCount=3,
)
_fh.setFormatter(logging.Formatter("%(asctime)s [engine] %(levelname)s %(message)s"))

logger = logging.getLogger("trading_bot.engine")
logger.setLevel(logging.INFO)
logger.addHandler(_fh)
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

    # Supervisor 接管所有持仓监控
    supervisor = PositionSupervisor(None)

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

        # ── 分段止盈 + 移动止损（委托给 PositionSupervisor）──
        try:
            from trading_bot.services.position_manager import load_bot_state, save_bot_state, market_close_position as _mcp
            from trading_bot.integrations.notifications import notify_exit
            from trading_bot.strategy.scalper import record_trade_result
            state = load_bot_state()
            decisions, state, changed = supervisor.evaluate_all(state)
            for d in decisions:
                if _mcp(d.symbol, d.side, d.qty):
                    try:
                        record_trade_result(d.pnl)
                        notify_exit(d.symbol, d.side, d.price, d.pnl, f'{d.action}')
                    except Exception:
                        pass
            if changed:
                save_bot_state(state)
        except Exception:
            logger.exception("position supervisor cycle failed")

        # ── 插针狙击检查 (实时) ──
        try:
            from trading_bot.strategy.scalper import _check_snipe_watch, _SNIPE_WATCH
            sniped = _check_snipe_watch()
            if sniped:
                logger.info(f"🔫 检测到 {len(sniped)} 个插针信号: {sniped}")
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

        # ── 每5分钟清理孤儿条件单 ──
        if loop_count % 600 == 0:
            try:
                import os, requests, hmac, hashlib, time as _t
                k = os.environ['BINANCE_API_KEY']; s = os.environ['BINANCE_API_SECRET']
                ts = int(_t.time()*1e3)
                q = f'timestamp={ts}'
                sig = hmac.new(s.encode(), q.encode(), hashlib.sha256).hexdigest()
                hdr = {'X-MBX-APIKEY': k}
                base = 'https://fapi.binance.com'
                algo = requests.get(f'{base}/fapi/v1/openAlgoOrders?{q}&signature={sig}', headers=hdr, timeout=10).json()
                pos = requests.get(f'{base}/fapi/v2/positionRisk?{q}&signature={sig}', headers=hdr, timeout=10).json()
                pos_syms = {p['symbol'] for p in pos if abs(float(p.get('positionAmt', 0))) > 0}
                cancelled = 0
                for a in (algo if isinstance(algo, list) else []):
                    sym = a.get('symbol', '')
                    if sym not in pos_syms:
                        aid = a.get('algoId', 0)
                        q2 = f'symbol={sym}&algoId={aid}&timestamp={int(_t.time()*1e3)}'
                        sig2 = hmac.new(s.encode(), q2.encode(), hashlib.sha256).hexdigest()
                        r = requests.delete(f'{base}/fapi/v1/algoOrder?{q2}&signature={sig2}', headers=hdr, timeout=10)
                        if r.status_code == 200:
                            logger.info(f'🧹 orphan: {sym} {a.get("orderType","")} id={aid}')
                            cancelled += 1
                        _t.sleep(0.1)  # rate limit
                if cancelled:
                    logger.info(f'🧹 清理完成: {cancelled} orphan(s)')
            except Exception as e:
                logger.warning(f'🧹 orphan cleanup error: {e}')

        time.sleep(0.5)

    ws_client.stop()
    logger.info("engine stopped")

# ⚠️ FROZEN: legacy module. Only fatal security fixes allowed. No new features.
from __future__ import annotations

import logging
import os
import signal
import time
import threading

from trading_bot.core.runtime_config import get_runtime_config
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

# 配置
SCAN_INTERVAL = 30       # 全市场扫描间隔（秒）
MANAGE_INTERVAL = 10     # 持仓管理间隔（秒）
CANDIDATE_UPDATE = 5     # 候选池更新间隔（秒）
UNIVERSE_SIZE = 100      # 全市场监控币数


def _stop(*_args):
    global _STOP
    _STOP = True


def _start_ws():
    """启动 WebSocket 客户端，订阅全市场 ticker + kline"""
    try:
        # 获取 Top100 交易量币种列表（REST）
        from trading_bot.strategy.market_regime import scan_top_coins
        top_coins, _ = scan_top_coins(min_volume_usdt=500000, max_coins=UNIVERSE_SIZE, top_n=UNIVERSE_SIZE)
        universe_symbols = [c['symbol'] for c in top_coins[:UNIVERSE_SIZE]]
        universe_symbols.append('BTCUSDT')
        universe_symbols = list(dict.fromkeys(universe_symbols))  # 去重

        if universe_symbols:
            ws_client.subscribe_tickers(universe_symbols)
            ws_client.subscribe_klines(universe_symbols, '1m')  # 全部100币
            ws_client.start()
            logger.info(f"WS started: {len(universe_symbols)} tickers + {len(universe_symbols)} klines")
            return universe_symbols
    except Exception as e:
        logger.warning(f"WS startup failed, using REST fallback: {e}")
    return []


def run() -> None:
    if os.getenv("LIVE_TRADING_ENABLED") != "YES":
        raise RuntimeError("LIVE_TRADING_ENABLED=YES is required")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    next_manage = 0.0
    next_scan = 0.0
    next_candidate = 0.0
    candidate_symbols = []

    logger.info("engine starting (WS+REST hybrid mode)")

    # 异步启动 WS（不阻塞主循环）
    ws_thread = threading.Thread(target=_start_ws, daemon=True)
    ws_thread.start()

    while not _STOP:
        cfg = get_runtime_config()
        now_ns = time.monotonic_ns()
        now_s = now_ns / 1_000_000_000

        # ── 处理执行队列（最高优先级，每次循环都处理）──
        execution_queue.process_all(max_count=5)

        # ── 更新增量特征（从WS缓存同步到增量存储）──
        try:
            for sym in candidate_symbols[:10]:
                bt = market_cache.get_book_ticker(sym)
                if bt:
                    feature_store.update_book(sym, bt['b'], bt['a'], bt['B'], bt['A'])
        except Exception:
            pass

        # ── 持仓管理（10s）──
        if now_s >= next_manage:
            try:
                run_full_cycle()
            except Exception:
                logger.exception("position management cycle failed")
            next_manage = now_s + MANAGE_INTERVAL

        # ── 策略扫描（30s）──
        if now_s >= next_scan:
            if cfg.get("strategy", {}).get("enabled", True):
                try:
                    if ensure_connection():
                        scalper.apply_runtime_config(cfg)
                        scalper.run_scalper()
                        candidate_symbols = list(candidate_pool.symbols)
                        if candidate_symbols and len(candidate_symbols) >= 3:
                            ws_client.subscribe_candidate(candidate_symbols[:5])
                    else:
                        logger.error("Binance unavailable; scan skipped")
                except Exception:
                    logger.exception("strategy cycle failed")
            next_scan = now_s + SCAN_INTERVAL

        # ── 健康检查（每30s）──
        if int(now_s) % 30 == 0 and int(now_s) != getattr(run, '_last_health', 0):
            run._last_health = int(now_s)
            tickers = market_cache.active_ticker_count
            klines = market_cache.active_kline_count
            eq_stats = execution_queue.stats
            logger.info(f"health: tickers={tickers} klines={klines} stale={market_cache.stale_count} "
                       f"queue={eq_stats['queue_size']} inflight={eq_stats['in_flight']} "
                       f"exec={eq_stats['total_executed']} drop={eq_stats['total_dropped']}")

        time.sleep(1.0)

    ws_client.stop()
    logger.info("engine stopped")

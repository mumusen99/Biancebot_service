"""WebSocket 多流市场数据客户端。分层订阅 + 事件总线 + 自动重连。"""
from __future__ import annotations
import json
import time
import threading
import logging
from typing import Callable, Optional
from collections import defaultdict

import websocket

logger = logging.getLogger(__name__)

from trading_bot.core.env_config import get_exchange_config

# Binance WS endpoints — 从统一环境配置读取（惰性）
try:
    _cfg = get_exchange_config()
    WS_BASE = _cfg.ws_base_url
except Exception:
    WS_BASE = "wss://fstream.binance.com/ws"
STREAM_LIMIT = 100  # 单连接最大流数（超过会被静默拒绝）


class MarketEvent:
    """统一市场事件"""
    __slots__ = ('stream', 'symbol', 'exchange_ts', 'receive_ts', 'payload')
    def __init__(self, stream: str, symbol: str, exchange_ts: int, payload: dict):
        self.stream = stream
        self.symbol = symbol
        self.exchange_ts = exchange_ts
        self.receive_ts = int(time.time() * 1000)
        self.payload = payload

    @property
    def latency_ms(self) -> int:
        return self.receive_ts - self.exchange_ts


class MarketCache:
    """内存市场数据缓存（线程安全）"""
    def __init__(self):
        self._lock = threading.Lock()
        self._tickers: dict[str, dict] = {}       # symbol → ticker
        self._klines_1m: dict[str, list] = defaultdict(list)  # symbol → [kline]
        self._book_tickers: dict[str, dict] = {}   # symbol → {bid, ask, bid_qty, ask_qty}
        self._agg_trades: dict[str, list] = defaultdict(list)  # symbol → [trade]
        self._depth: dict[str, dict] = {}          # symbol → {bids, asks}
        self._listeners: dict[str, list[Callable]] = defaultdict(list)
        self._last_update: dict[str, float] = {}

    def update_ticker(self, symbol: str, data: dict):
        with self._lock:
            # 增量更新：不覆盖已有字段
            existing = self._tickers.get(symbol, {})
            existing.update(data)
            self._tickers[symbol] = existing
            self._last_update[f"ticker_{symbol}"] = time.time()

    def add_kline(self, symbol: str, kline: dict, is_closed: bool = False):
        with self._lock:
            klines = self._klines_1m[symbol]
            kline['_closed'] = is_closed
            if klines and klines[-1].get('t') == kline['t']:
                klines[-1] = kline  # 更新未闭合K线
            else:
                klines.append(kline)
                if len(klines) > 120:
                    klines.pop(0)
            self._last_update[f"kline_{symbol}"] = time.time()

    def update_book_ticker(self, symbol: str, data: dict):
        with self._lock:
            self._book_tickers[symbol] = data
            self._last_update[f"book_{symbol}"] = time.time()

    def add_agg_trade(self, symbol: str, trade: dict, maxlen: int = 200):
        with self._lock:
            trades = self._agg_trades[symbol]
            trades.append(trade)
            if len(trades) > maxlen:
                trades.pop(0)

    def update_depth(self, symbol: str, data: dict):
        with self._lock:
            self._depth[symbol] = data

    def get_ticker(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return self._tickers.get(symbol)

    def get_klines(self, symbol: str, limit: int = 60) -> list:
        with self._lock:
            klines = self._klines_1m.get(symbol, [])
            return klines[-limit:] if klines else []

    def get_book_ticker(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return self._book_tickers.get(symbol)

    def get_spread_pct(self, symbol: str) -> Optional[float]:
        bt = self.get_book_ticker(symbol)
        if bt and bt.get('b') and bt.get('a'):
            return (float(bt['a']) - float(bt['b'])) / float(bt['b']) * 100
        return None

    def get_buy_sell_ratio(self, symbol: str, n: int = 50) -> float:
        with self._lock:
            trades = self._agg_trades.get(symbol, [])[-n:]
            if not trades:
                return 1.0
            buy_vol = sum(float(t['q']) for t in trades if not t.get('m'))
            sell_vol = sum(float(t['q']) for t in trades if t.get('m'))
            return buy_vol / sell_vol if sell_vol > 0 else 1.0

    def is_stale(self, symbol: str, max_age: float = 5.0) -> bool:
        return time.time() - self._last_update.get(f"ticker_{symbol}", 0) > max_age

    def aggregate_5m(self, symbol: str) -> Optional[dict]:
        """从1m K线聚合成5m OHLCV"""
        with self._lock:
            klines = self._klines_1m.get(symbol, [])
            if len(klines) < 5:
                return None
            # 取最近5根已闭合的1m K线
            closed = [k for k in klines if k.get('_closed')]
            if len(closed) < 5:
                return None
            last5 = closed[-5:]
            return {
                't': last5[0]['t'],
                'o': last5[0]['o'],
                'h': max(k['h'] for k in last5),
                'l': min(k['l'] for k in last5),
                'c': last5[-1]['c'],
                'v': sum(k['v'] for k in last5),
            }

    def get_klines_df(self, symbol: str, interval: str = '5m', limit: int = 60):
        """返回 pandas DataFrame 格式的K线数据"""
        import pandas as pd
        if interval == '1m':
            bars = self.get_klines(symbol, limit)
        elif interval == '5m':
            bars = []
            for _ in range(min(limit, 60)):
                agg = self.aggregate_5m(symbol)
                if agg:
                    bars.append(agg)
            bars = bars[-limit:]
        else:
            bars = []
        if not bars:
            return None
        df = pd.DataFrame(bars)
        if not df.empty:
            df = df.rename(columns={'t': 'timestamp', 'o': 'open', 'h': 'high',
                                     'l': 'low', 'c': 'close', 'v': 'volume'})
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df

    @property
    def active_ticker_count(self) -> int:
        return len(self._book_tickers)

    @property
    def active_kline_count(self) -> int:
        return len(self._klines_1m)

    @property
    def stale_count(self, max_age: float = 10.0) -> int:
        now = time.time()
        return sum(1 for t in self._last_update.values() if now - t > max_age)

    def is_fresh(self, symbol: str, *, max_age: float = 5.0) -> bool:
        """检查symbol数据是否新鲜。用于拒绝基于过期数据的开仓。"""
        keys = [f"ticker_{symbol}", f"kline_{symbol}"]
        now = time.time()
        for k in keys:
            t = self._last_update.get(k, 0)
            if now - t > max_age:
                return False
        return True

    def last_age(self, symbol: str) -> float:
        """symbol最近数据距今秒数。"""
        keys = [f"ticker_{symbol}", f"kline_{symbol}"]
        now = time.time()
        ages = [now - self._last_update.get(k, 0) for k in keys if k in self._last_update]
        return max(ages) if ages else 999.0

    def detect_gap(self, symbol: str, max_gap_ms: int = 120000) -> Optional[tuple[int, int]]:
        """检测K线缺口。返回 (last_close_ms, now_ms) 或 None"""
        with self._lock:
            klines = self._klines_1m.get(symbol, [])
            if not klines:
                return None
            closed = [k for k in klines if k.get('_closed')]
            if len(closed) < 2:
                return None
            last_close = closed[-1].get('T', 0)
            now_ms = int(time.time() * 1000)
            gap = now_ms - last_close
            if gap > max_gap_ms:
                return (last_close, now_ms)
        return None


# 全局缓存实例
market_cache = MarketCache()


class BinanceWSClient:
    """Binance WebSocket 客户端，支持分层订阅管理"""

    def __init__(self):
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._streams: dict[str, set[str]] = defaultdict(set)  # layer → {streams}
        self._running = False
        self._reconnect_delay = 1.0

    def _make_stream_name(self, symbol: str, channel: str) -> str:
        sym = symbol.lower()
        return f"{sym}@{channel}"

    def subscribe_tickers(self, symbols: list[str]):
        """订阅 bookTicker（1s检查用实时买卖价）"""
        streams = [self._make_stream_name(s, "bookTicker") for s in symbols]
        self._streams['universe'].update(streams)
        self._resubscribe()

    def subscribe_klines(self, symbols: list[str], interval: str = "1m"):
        """订阅 K 线"""
        streams = [self._make_stream_name(s, f"kline_{interval}") for s in symbols]
        self._streams['universe'].update(streams)
        self._resubscribe()

    def subscribe_candidate(self, symbols: list[str]):
        """候选池层：aggTrade + bookTicker + kline_1m"""
        for ch in ("aggTrade", "bookTicker", "kline_1m"):
            streams = [self._make_stream_name(s, ch) for s in symbols]
            self._streams['candidate'].update(streams)
        self._resubscribe()

    def subscribe_depth(self, symbols: list[str], speed: str = "250ms"):
        """深度层"""
        streams = [self._make_stream_name(s, f"depth@{speed}") for s in symbols]
        self._streams['deep'].update(streams)
        self._resubscribe()

    def unsubscribe_layer(self, layer: str):
        """取消某层所有订阅"""
        self._streams.pop(layer, None)
        self._resubscribe()

    def _resubscribe(self):
        """重建连接"""
        if not self._running:
            return
        all_streams = []
        for streams in self._streams.values():
            all_streams.extend(streams)
        if not all_streams:
            return
        # 超过200条流需要分多个连接，这里简化为取前200
        all_streams = all_streams[:STREAM_LIMIT]
        stream_str = "/".join(all_streams)
        if len(all_streams) == 1:
            url = f"wss://fstream.binance.com/ws/{stream_str}"
        else:
            url = f"wss://fstream.binance.com/stream?streams={stream_str}"
        logger.info(f"WS connecting: {len(all_streams)} streams")
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )

    def _on_open(self, ws):
        logger.info("WS connected")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if 'stream' in data:
                self._dispatch(data['stream'], data['data'])
            elif 'e' in data:
                self._dispatch_raw(data)
        except Exception as e:
            logger.warning(f"WS message error: {e}")

    def _dispatch(self, stream: str, data: dict):
        parts = stream.split('@')
        symbol = parts[0].upper()
        channel = parts[1] if len(parts) > 1 else ''
        ts = data.get('E', int(time.time() * 1000))

        if 'miniTicker' in channel or 'ticker' in channel:
            market_cache.update_ticker(symbol, data)
        elif 'kline' in channel:
            k = data.get('k', data)
            market_cache.add_kline(symbol, {
                't': k.get('t', ts), 'o': float(k.get('o', 0)),
                'h': float(k.get('h', 0)), 'l': float(k.get('l', 0)),
                'c': float(k.get('c', 0)), 'v': float(k.get('v', 0)),
                'T': k.get('T', ts),
            }, k.get('x', False))
        elif 'bookTicker' in channel:
            market_cache.update_book_ticker(symbol, {
                'b': float(data.get('b', 0)), 'a': float(data.get('a', 0)),
                'B': float(data.get('B', 0)), 'A': float(data.get('A', 0)),
            })
        elif 'aggTrade' in channel:
            market_cache.add_agg_trade(symbol, {
                'p': float(data.get('p', 0)), 'q': float(data.get('q', 0)),
                'm': data.get('m', False), 'T': ts,
            })
        elif 'depth' in channel:
            market_cache.update_depth(symbol, {
                'bids': data.get('bids', []), 'asks': data.get('asks', []),
            })

    def _dispatch_raw(self, data: dict):
        event_type = data.get('e', '')
        symbol = data.get('s', '').upper()
        if event_type == '24hrTicker':
            market_cache.update_ticker(symbol, data)

    def _on_error(self, ws, error):
        logger.warning(f"WS error: {error}")

    def _on_close(self, ws, code, msg):
        logger.warning(f"WS closed: code={code} msg={msg}")
        if self._running:
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 1.5, 30)
            self._resubscribe()
            if self._ws:
                self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
                self._thread.start()

    def start(self):
        """启动 WebSocket 客户端"""
        self._running = True
        self._resubscribe()
        if self._ws:
            self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
            self._thread.start()
            logger.info("WS client started")

    def stop(self):
        """停止"""
        self._running = False
        if self._ws:
            self._ws.close()


# 全局客户端实例
ws_client = BinanceWSClient()

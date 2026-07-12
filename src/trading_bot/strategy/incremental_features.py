"""增量特征：只更新受当前事件影响的字段，不全量重算。"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Optional


@dataclass
class IncrementalFeatures:
    """每个 symbol 的短周期增量状态"""
    symbol: str = ""

    # 盘口
    best_bid: float = 0.0
    best_ask: float = 0.0
    best_bid_qty: float = 0.0
    best_ask_qty: float = 0.0
    spread_bps: float = 0.0
    book_imbalance: float = 0.0
    micro_price: float = 0.0

    # 成交流
    buy_volume_1s: float = 0.0
    sell_volume_1s: float = 0.0
    buy_sell_ratio_1s: float = 1.0

    # 价格
    last_price: float = 0.0
    vwap_1s: float = 0.0

    # 元数据
    signal_age_ms: int = 0
    last_update_ns: int = 0

    # 内部追踪（不序列化）
    _trade_window: deque = field(default_factory=lambda: deque(maxlen=200))
    _price_window: deque = field(default_factory=lambda: deque(maxlen=60))

    def update_book_ticker(self, bid: float, ask: float, bid_qty: float, ask_qty: float):
        self.best_bid = bid
        self.best_ask = ask
        self.best_bid_qty = bid_qty
        self.best_ask_qty = ask_qty

        mid = (bid + ask) / 2
        self.spread_bps = ((ask - bid) / mid) * 10000 if mid > 0 else 0

        total = bid_qty + ask_qty
        if total > 0:
            self.book_imbalance = (bid_qty - ask_qty) / total
            self.micro_price = (ask * bid_qty + bid * ask_qty) / total

        self.last_update_ns = time.monotonic_ns()

    def add_trade(self, price: float, qty: float, is_buyer_maker: bool):
        now = time.monotonic_ns()
        cutoff = now - 1_000_000_000  # 1 秒窗口

        self._trade_window.append((now, price, qty, is_buyer_maker))
        self._price_window.append((now, price, qty))

        # 清理旧数据
        while self._trade_window and self._trade_window[0][0] < cutoff:
            self._trade_window.popleft()
        while self._price_window and self._price_window[0][0] < cutoff:
            self._price_window.popleft()

        self.last_price = price

        # 计算买卖比
        buy_vol = sum(t[2] for t in self._trade_window if not t[3])
        sell_vol = sum(t[2] for t in self._trade_window if t[3])
        self.buy_volume_1s = buy_vol
        self.sell_volume_1s = sell_vol
        self.buy_sell_ratio_1s = buy_vol / sell_vol if sell_vol > 0 else 1.0

        # VWAP
        total_qty = sum(t[2] for t in self._price_window)
        if total_qty > 0:
            self.vwap_1s = sum(t[1] * t[2] for t in self._price_window) / total_qty

        self.last_update_ns = now

    def is_order_flow_bullish(self) -> bool:
        return self.buy_sell_ratio_1s > 1.25 and self.book_imbalance > -0.2

    def is_order_flow_bearish(self) -> bool:
        return self.buy_sell_ratio_1s < 0.75 and self.book_imbalance < 0.2

    def is_spread_normal(self, max_bps: float = 5.0) -> bool:
        return self.spread_bps < max_bps


class IncrementalFeatureStore:
    """全局增量特征存储"""

    def __init__(self):
        self._features: dict[str, IncrementalFeatures] = {}

    def get(self, symbol: str) -> IncrementalFeatures:
        if symbol not in self._features:
            self._features[symbol] = IncrementalFeatures(symbol=symbol)
        return self._features[symbol]

    def update_book(self, symbol: str, bid: float, ask: float, bid_qty: float, ask_qty: float):
        f = self.get(symbol)
        f.update_book_ticker(bid, ask, bid_qty, ask_qty)

    def update_trade(self, symbol: str, price: float, qty: float, is_buyer_maker: bool):
        f = self.get(symbol)
        f.add_trade(price, qty, is_buyer_maker)

    def get_buy_sell_ratio(self, symbol: str) -> float:
        return self.get(symbol).buy_sell_ratio_1s


# 全局实例
feature_store = IncrementalFeatureStore()

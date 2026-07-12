"""符号规则：统一处理 tickSize/stepSize/minQty/minNotional，禁止各模块自行 round。"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SymbolRules:
    symbol: str
    tick_size: float = 0.01
    step_size: float = 0.001
    min_qty: float = 0.001
    min_notional: float = 5.0
    price_precision: int = 2
    qty_precision: int = 3

    @classmethod
    def from_exchange_info(cls, symbol: str, exchange_info: dict) -> SymbolRules:
        """从 exchangeInfo 创建"""
        for s in exchange_info.get('symbols', []):
            if s.get('symbol', '').upper() == symbol.upper():
                filters = {f['filterType']: f for f in s.get('filters', [])}
                price_f = filters.get('PRICE_FILTER', {})
                lot_f = filters.get('LOT_SIZE', {})
                min_notional_f = filters.get('MIN_NOTIONAL', {})
                return cls(
                    symbol=symbol.upper(),
                    tick_size=float(price_f.get('tickSize', 0.01)),
                    step_size=float(lot_f.get('stepSize', 0.001)),
                    min_qty=float(lot_f.get('minQty', 0.001)),
                    min_notional=float(min_notional_f.get('notional', 5.0)),
                    price_precision=_decimal_places(price_f.get('tickSize', '0.01')),
                    qty_precision=_decimal_places(lot_f.get('stepSize', '0.001')),
                )
        return cls(symbol=symbol.upper())

    def round_price(self, price: float, side: str = '') -> float:
        """价格取整。LONG向下取，SHORT向上取"""
        if side.upper() == 'LONG':
            return int(price / self.tick_size) * self.tick_size
        elif side.upper() == 'SHORT':
            return int((price + self.tick_size * 0.999) / self.tick_size) * self.tick_size
        return round(price / self.tick_size) * self.tick_size

    def round_qty(self, qty: float) -> float:
        """数量向下取整"""
        return int(qty / self.step_size) * self.step_size

    def format_price(self, price: float) -> str:
        return f'{self.round_price(price):.{self.price_precision}f}'

    def format_qty(self, qty: float) -> str:
        qty = self.round_qty(qty)
        return f'{qty:.{self.qty_precision}f}'


def _decimal_places(tick_str: str) -> int:
    """'0.01' → 2, '0.001' → 3, '1' → 0"""
    if '.' in tick_str:
        return len(tick_str.split('.')[1].rstrip('0'))
    return 0


class SymbolRulesRegistry:
    """全市场规则注册表"""

    def __init__(self):
        self._rules: dict[str, SymbolRules] = {}
        self._loaded = False

    def load(self, exchange_info: dict):
        for s in exchange_info.get('symbols', []):
            sym = s.get('symbol', '').upper()
            self._rules[sym] = SymbolRules.from_exchange_info(sym, exchange_info)
        self._loaded = True
        logger.info(f'loaded {len(self._rules)} symbol rules')

    def get(self, symbol: str) -> SymbolRules:
        sym = symbol.upper()
        if sym not in self._rules:
            logger.warning(f'no rules for {sym}, using defaults')
            return SymbolRules(symbol=sym)
        return self._rules[sym]


# 全局注册表
symbol_rules = SymbolRulesRegistry()

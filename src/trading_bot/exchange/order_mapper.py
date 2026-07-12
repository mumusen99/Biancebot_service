"""
订单映射 — Binance API 响应 ↔ 内部数据模型。
策略层只使用这些内部类型，不接触 Binance 原始字段。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Optional


class OrderRole(StrEnum):
    """订单角色 — 标记每笔订单的业务目的。"""
    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    SCALE_IN = "SCALE_IN"
    SCALE_OUT = "SCALE_OUT"


class OrderStatus(StrEnum):
    """内部订单状态。"""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class PositionSide(StrEnum):
    """持仓方向。"""
    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(StrEnum):
    """订单类型。"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    TRAILING_STOP_MARKET = "TRAILING_STOP_MARKET"


class WorkingType(StrEnum):
    """触发价格类型。"""
    MARK_PRICE = "MARK_PRICE"
    CONTRACT_PRICE = "CONTRACT_PRICE"


# ─── 请求模型 ─────────────────────────────────


@dataclass(frozen=True, slots=True)
class EntryOrderRequest:
    """开仓订单请求。"""
    symbol: str
    side: PositionSide
    quantity: Decimal
    order_type: OrderType = OrderType.MARKET
    price: Optional[Decimal] = None        # 限价单价格
    leverage: int = 3
    reduce_only: bool = False
    client_order_id: str = field(default_factory=lambda: _gen_cid("entry"))


@dataclass(frozen=True, slots=True)
class ExitOrderRequest:
    """平仓订单请求。"""
    symbol: str
    side: PositionSide              # 持仓方向
    quantity: Decimal               # 平仓数量（None = 全平）
    order_type: OrderType = OrderType.MARKET
    price: Optional[Decimal] = None
    reduce_only: bool = True
    client_order_id: str = field(default_factory=lambda: _gen_cid("exit"))


@dataclass(frozen=True, slots=True)
class ProtectionOrderRequest:
    """保护单（止损/止盈）请求。"""
    symbol: str
    position_side: PositionSide
    role: OrderRole                     # STOP_LOSS / TAKE_PROFIT / TRAILING_STOP
    trigger_price: Decimal
    quantity: Optional[Decimal] = None  # None = 全仓
    close_position: bool = False        # True = 全平
    reduce_only: bool = True
    working_type: WorkingType = WorkingType.MARK_PRICE
    price_protect: bool = True
    client_order_id: str = field(default_factory=lambda: _gen_cid("prot"))


# ─── 响应模型 ─────────────────────────────────


@dataclass(frozen=True, slots=True)
class OrderResult:
    """下单结果。"""
    symbol: str
    order_id: int
    client_order_id: str
    status: OrderStatus
    executed_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    position_side: PositionSide = PositionSide.LONG
    side: str = "BUY"               # Binance 原始 side (BUY/SELL)
    raw_response: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class CancelResult:
    """撤单结果。"""
    symbol: str
    order_id: str                    # algoId 或 orderId
    client_order_id: str
    success: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ExchangeOrder:
    """交易所订单（查询结果）。"""
    symbol: str
    order_id: int
    client_order_id: str
    order_type: OrderType
    status: OrderStatus
    side: str                        # BUY / SELL
    position_side: PositionSide
    price: Decimal = Decimal("0")
    stop_price: Decimal = Decimal("0")
    orig_qty: Decimal = Decimal("0")
    executed_qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    reduce_only: bool = False
    update_time: int = 0


@dataclass(frozen=True, slots=True)
class ExchangePosition:
    """交易所仓位。"""
    symbol: str
    position_side: PositionSide
    position_amt: Decimal            # 持仓数量（正=LONG, 负=SHORT）
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    isolated: bool
    notional: Decimal                # 名义价值
    liquidation_price: Decimal


# ─── 工具函数 ─────────────────────────────────

_PREFIX = "tb"  # 所有 clientOrderId 前缀

def _gen_cid(role: str) -> str:
    """生成确定性 clientOrderId: tb-{role}-{uuid_short}"""
    uid = uuid.uuid4().hex[:8]
    return f"{_PREFIX}-{role}-{uid}"


def make_client_order_id(symbol: str, side: PositionSide, role: OrderRole,
                         position_id: str = "", seq: int = 0) -> str:
    """生成结构化 clientOrderId: tb-{symbol}-{side}-{role}-{pid}-{seq}

    例如: tb-BTCUSDT-L-SL-a91f3c-01
    """
    sym_short = symbol.replace("USDT", "")
    side_short = side.value[0]  # L / S
    role_short = role.value[:4] if len(role.value) > 4 else role.value  # STOP / TAKE / TRAIL / ENTR / SCAL
    pid_short = position_id[:6] if position_id else "000000"
    seq_str = f"{seq:02d}"
    return f"{_PREFIX}-{sym_short}-{side_short}-{role_short}-{pid_short}-{seq_str}"


def map_binance_order(raw: dict, symbol: str) -> ExchangeOrder:
    """将 Binance 订单响应映射为 ExchangeOrder。"""
    return ExchangeOrder(
        symbol=symbol,
        order_id=raw.get("orderId", 0),
        client_order_id=raw.get("clientOrderId", ""),
        order_type=OrderType(raw.get("orderType") or raw.get("type", "MARKET")),
        status=OrderStatus({"NEW": "SUBMITTED", "FILLED": "FILLED", "PARTIALLY_FILLED": "PARTIALLY_FILLED",
                            "CANCELED": "CANCELLED", "EXPIRED": "EXPIRED", "REJECTED": "REJECTED"}.get(
                            raw.get("algoStatus") or raw.get("status", "NEW"), "SUBMITTED")),
        side=raw.get("side", "BUY"),
        position_side=PositionSide.LONG if raw.get("positionSide", "LONG") == "LONG" else PositionSide.SHORT,
        price=Decimal(str(raw.get("price", 0))),
        stop_price=Decimal(str(raw.get("stopPrice", 0))),
        orig_qty=Decimal(str(raw.get("origQty", 0))),
        executed_qty=Decimal(str(raw.get("executedQty", 0))),
        avg_price=Decimal(str(raw.get("avgPrice", 0))),
        reduce_only=raw.get("reduceOnly", False),
        update_time=raw.get("updateTime", 0),
    )


def map_binance_position(raw: dict) -> ExchangePosition:
    """将 Binance positionRisk 响应映射为 ExchangePosition。"""
    return ExchangePosition(
        symbol=raw.get("symbol", ""),
        position_side=PositionSide.LONG if raw.get("positionSide", "LONG") == "LONG" else PositionSide.SHORT,
        position_amt=Decimal(str(raw.get("positionAmt", 0))),
        entry_price=Decimal(str(raw.get("entryPrice", 0))),
        mark_price=Decimal(str(raw.get("markPrice", 0))),
        unrealized_pnl=Decimal(str(raw.get("unRealizedProfit", 0))),
        leverage=int(raw.get("leverage", 1)),
        isolated=raw.get("isolated", False),
        notional=Decimal(str(raw.get("notional", 0))),
        liquidation_price=Decimal(str(raw.get("liquidationPrice", 0))),
    )


def map_order_result(raw: dict) -> OrderResult:
    """将 Binance 下单 / algoOrder 响应映射为 OrderResult。"""
    status = raw.get("status", raw.get("orderStatus", "NEW"))
    status_map = {
        "NEW": OrderStatus.SUBMITTED,
        "FILLED": OrderStatus.FILLED,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "CANCELED": OrderStatus.CANCELLED,
        "EXPIRED": OrderStatus.EXPIRED,
        "REJECTED": OrderStatus.REJECTED,
    }
    return OrderResult(
        symbol=raw.get("symbol", ""),
        order_id=raw.get("orderId", raw.get("algoId", 0)),
        client_order_id=raw.get("clientOrderId", raw.get("clientAlgoId", "")),
        status=status_map.get(status, OrderStatus.SUBMITTED),
        executed_qty=Decimal(str(raw.get("executedQty", 0))),
        avg_price=Decimal(str(raw.get("avgPrice", 0))),
        position_side=PositionSide.LONG if raw.get("positionSide", "LONG") == "LONG" else PositionSide.SHORT,
        side=raw.get("side", "BUY"),
        raw_response=raw,
    )

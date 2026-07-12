"""唯一保护单模块：创建/替换/校验止损止盈。scalper和position_manager只能调这里。"""
from __future__ import annotations
import time, hmac, hashlib, urllib.parse, logging
from dataclasses import dataclass
import requests as req

from trading_bot.core.settings import API_KEY, API_SECRET, PROXY
from trading_bot.core.env_config import get_exchange_config, is_testnet
from trading_bot.exchange.client import _get_symbol_precision, _load_precisions

logger = logging.getLogger(__name__)

try:
    _cfg = get_exchange_config()
    FAPI_BASE = _cfg.fapi_v1_base
    IS_TESTNET = is_testnet()
except Exception:
    FAPI_BASE = "https://fapi.binance.com/fapi/v1"
    IS_TESTNET = False
_session = req.Session()
_session.proxies = {"http": PROXY, "https": PROXY}


@dataclass(frozen=True)
class ProtectionResult:
    stop_ok: bool
    take_profit_ok: bool
    stop_algo_id: int | None = None
    take_profit_algo_id: int | None = None
    reason: str = ""


def _align_price_dir(symbol: str, price: float, direction: str) -> float:
    """保守方向取整：LONG止损down止盈up，SHORT止损up止盈down"""
    from trading_bot.exchange.client import _load_precisions, _get_symbol_precision
    _load_precisions()
    _, _, _, tick = _get_symbol_precision(symbol)
    if direction == "down":
        return float(int(price / tick) * tick)  # 向下取整
    elif direction == "up":
        val = int(price / tick) * tick
        if val < price:
            val += tick
        return float(val)
    return float(round(price / tick) * tick)


def align_protection_prices(symbol: str, side: str, sl: float, tp: float) -> tuple[float, float]:
    """按方向取整保护价格"""
    if side == "LONG":
        return (
            _align_price_dir(symbol, sl, "down"),
            _align_price_dir(symbol, tp, "up"),
        )
    return (
        _align_price_dir(symbol, sl, "up"),
        _align_price_dir(symbol, tp, "down"),
    )


def validate_protection_side(side: str, mark: float, sl: float, tp: float | None) -> None:
    """校验止损在Mark Price正确一侧"""
    if side == "LONG":
        if not sl < mark:
            raise ValueError(f"LONG stop must be below mark: sl={sl}, mark={mark}")
        if tp is not None and not mark < tp:
            raise ValueError(f"LONG tp must be above mark: tp={tp}, mark={mark}")
    else:
        if not sl > mark:
            raise ValueError(f"SHORT stop must be above mark: sl={sl}, mark={mark}")
        if tp is not None and not mark > tp:
            raise ValueError(f"SHORT tp must be below mark: tp={tp}, mark={mark}")


from trading_bot.exchange.gateway import get_gateway
_gw = get_gateway()

def _place_algo_order(symbol: str, side: str, pos_side: str,
                       ord_type: str, qty: str, trigger: float) -> dict:
    """挂条件单 — 委托给 ExchangeGateway。"""
    aligned_trigger = _align_price_dir(symbol, trigger, "nearest")
    rid = _gw._request_id()
    return _gw._call("POST", _gw._fapi_v1, "algoOrder", {
        "symbol": symbol, "side": side, "positionSide": pos_side,
        "type": ord_type, "quantity": qty,
        "triggerprice": str(aligned_trigger),
        "workingType": "MARK_PRICE", "reduceOnly": "true",
        "algotype": "CONDITIONAL",
        "newClientOrderId": rid,
    }, rid, symbol)


def _cancel_algo(symbol: str, algo_id: int) -> bool:
    """删除条件单 — 委托给 ExchangeGateway。"""
    try:
        result = _gw.cancel_algo_order(symbol, str(algo_id))
        return result.success
    except Exception:
        return False


def _get_algo_orders(symbol: str) -> list:
    """查询条件委托 — 委托给 ExchangeGateway。"""
    try:
        rid = _gw._request_id()
        data = _gw._call("GET", _gw._fapi_v1, "allAlgoOrders",
                        {"symbol": symbol}, rid, symbol)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def ensure_position_protection(
    *,
    symbol: str,
    position_side: str,
    actual_qty: float,
    stop_price: float,
    take_profit_price: float | None,
    mark_price: float,
    owner_tag: str = "",
) -> ProtectionResult:
    """确保持仓有正确的止盈止损。只创建/替换，不计算目标价。"""
    _load_precisions()
    _, step, _, _ = _get_symbol_precision(symbol)

    # 对齐数量
    qty_decimals = len(str(step).split(".")[-1]) if "." in str(step) else 0
    qty_aligned = int(actual_qty / step) * step
    qty_str = ("%g" % qty_aligned).replace(",", "")

    if float(qty_str) <= 0:
        return ProtectionResult(False, False, reason="zero qty")

    close_side = "SELL" if position_side == "LONG" else "BUY"

    # 对齐价格
    sl_aligned, tp_aligned = align_protection_prices(
        symbol, position_side, stop_price, take_profit_price or stop_price * 1.02
    )

    # 校验方向
    try:
        validate_protection_side(position_side, mark_price, sl_aligned,
                                 tp_aligned if take_profit_price else None)
    except ValueError as exc:
        return ProtectionResult(False, False, reason=str(exc))

    # 创建止损
    stop_ok = False
    stop_algo_id = None
    try:
        sl_order = _place_algo_order(symbol, close_side, position_side,
                                      "STOP_MARKET", qty_str, sl_aligned)
        stop_algo_id = int(sl_order.get("algoId", 0) or 0)
        stop_ok = bool(stop_algo_id)
    except Exception as exc:
        logger.error("stop order failed for %s: %s", symbol, exc)

    # 创建止盈
    tp_ok = False
    tp_algo_id = None
    if take_profit_price and stop_ok:
        try:
            tp_order = _place_algo_order(symbol, close_side, position_side,
                                          "TAKE_PROFIT_MARKET", qty_str, tp_aligned)
            tp_algo_id = int(tp_order.get("algoId", 0) or 0)
            tp_ok = bool(tp_algo_id)
        except Exception as exc:
            logger.warning("tp order failed for %s (stop is ok): %s", symbol, exc)

    return ProtectionResult(
        stop_ok=stop_ok, take_profit_ok=tp_ok,
        stop_algo_id=stop_algo_id, take_profit_algo_id=tp_algo_id,
        reason="ok" if stop_ok else "stop creation failed",
    )


def cancel_all_protection(symbol: str, position_side: str = None) -> int:
    """取消某币条件单，返回取消数。position_side 可选过滤方向。"""
    cancelled = 0
    for a in _get_algo_orders(symbol):
        if a.get("algoStatus") not in ("NEW", "WORKING"):
            continue
        if position_side and str(a.get("positionSide", "")).upper() != position_side.upper():
            continue
        if _cancel_algo(symbol, a["algoId"]):
            cancelled += 1
        time.sleep(0.15)
    return cancelled


def repair_existing_protection(pos: dict, live_position: dict) -> ProtectionResult:
    """校验并修复已有持仓的保护单（不重新计算目标价）。"""
    return ensure_position_protection(
        symbol=pos["symbol"],
        position_side=pos["side"],
        actual_qty=abs(float(live_position.get("positionAmt", 0))),
        stop_price=float(pos.get("sl_price", 0) or 0),
        take_profit_price=float(pos.get("tp_price", 0) or 0) or None,
        mark_price=float(live_position.get("markPrice", 0)),
        owner_tag=str(pos.get("entry_order_id", "")),
    )


def ensure_partial_tp_protection(
    *,
    symbol: str,
    position_side: str,
    actual_qty: float,
    entry_price: float,
    stop_price: float,
    tp1_r: float = 0.6,
    tp2_r: float = 1.2,
    runner_ratio: float = 0.20,
    mark_price: float = 0,
    owner_tag: str = "",
) -> ProtectionResult:
    """
    分批止盈保护: TP1(50%) + TP2(30%) + 20%跟踪。
    SL 覆盖全部数量，TP1/TP2 各自分仓。
    """
    _load_precisions()
    _, step, _, _ = _get_symbol_precision(symbol)
    qty_decimals = len(str(step).split(".")[-1]) if "." in str(step) else 0
    qty_aligned = int(actual_qty / step) * step

    if qty_aligned <= 0:
        return ProtectionResult(False, False, reason="zero qty")

    close_side = "SELL" if position_side == "LONG" else "BUY"
    risk_dist = abs(entry_price - stop_price)

    # TP1: 50% qty at entry + 0.6R
    tp1_price = entry_price + risk_dist * tp1_r if position_side == "LONG" else entry_price - risk_dist * tp1_r
    tp1_qty = int(qty_aligned * 0.50 / step) * step

    # TP2: 30% qty at entry + 1.2R
    tp2_price = entry_price + risk_dist * tp2_r if position_side == "LONG" else entry_price - risk_dist * tp2_r
    tp2_qty = int(qty_aligned * 0.30 / step) * step

    # SL: 100% qty
    sl_price_aligned, _ = align_protection_prices(symbol, position_side, stop_price, stop_price * 1.02)

    if mark_price > 0:
        try:
            validate_protection_side(position_side, mark_price, sl_price_aligned, None)
        except ValueError as exc:
            return ProtectionResult(False, False, reason=str(exc))

    # 创建止损（全量）
    sl_qty_str = ("%g" % qty_aligned).replace(",", "")
    stop_ok = False
    try:
        sl_order = _place_algo_order(symbol, close_side, position_side,
                                      "STOP_MARKET", sl_qty_str, sl_price_aligned)
        stop_ok = bool(sl_order.get("algoId"))
    except Exception as exc:
        logger.error("stop order failed for %s: %s", symbol, exc)
        return ProtectionResult(False, False, reason=str(exc))

    # 创建 TP1
    tp1_ok = False
    tp1_qty_str = ("%g" % max(tp1_qty, step)).replace(",", "")
    if float(tp1_qty_str) > 0:
        try:
            _, tp1_aligned = align_protection_prices(symbol, position_side, stop_price, tp1_price)
            _place_algo_order(symbol, close_side, position_side,
                               "TAKE_PROFIT_MARKET", tp1_qty_str, tp1_aligned)
            tp1_ok = True
        except Exception as exc:
            logger.warning("TP1 failed for %s: %s", symbol, exc)

    # 创建 TP2
    tp2_ok = False
    tp2_qty_str = ("%g" % max(tp2_qty, step)).replace(",", "")
    if float(tp2_qty_str) > 0:
        try:
            _, tp2_aligned = align_protection_prices(symbol, position_side, stop_price, tp2_price)
            _place_algo_order(symbol, close_side, position_side,
                               "TAKE_PROFIT_MARKET", tp2_qty_str, tp2_aligned)
            tp2_ok = True
        except Exception as exc:
            logger.warning("TP2 failed for %s: %s", symbol, exc)

    return ProtectionResult(
        stop_ok=stop_ok,
        take_profit_ok=tp1_ok or tp2_ok,
        reason=f"partial_tp tp1_ok={tp1_ok} tp2_ok={tp2_ok}",
    )

"""
ExchangeGateway — 所有交易所写操作的唯一出口。

策略层、仓位管理、风控层不得直接调用 Binance API。
所有写操作必须经过此网关。

设计原则 (per trading_bot_refactor_improvement_plan.md §P0-2):
1. 策略层不得出现 requests.get/post/delete、hmac、signature、Binance URL
2. 仓位管理层不得自行拼接 Binance 参数
3. 所有交易所错误统一映射成内部异常
4. 每次请求生成 request_id, 日志包含: request_id, symbol, position_side, client_order_id, operation, latency_ms, exchange_code
5. 所有写请求必须支持确定性 clientOrderId
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse
import uuid
from typing import Optional

import requests as req

from trading_bot.core.env_config import get_exchange_config
from trading_bot.core.settings import API_KEY, API_SECRET, PROXY
from trading_bot.exchange.errors import (
    ExchangeError, ExchangeTimeoutError, map_binance_error,
)
from trading_bot.exchange.order_mapper import (
    CancelResult, EntryOrderRequest, ExchangeOrder, ExchangePosition,
    ExitOrderRequest, OrderResult, ProtectionOrderRequest,
    make_client_order_id, map_binance_order, map_binance_position,
    map_order_result,
)

logger = logging.getLogger("trading_bot.gateway")


class ExchangeGateway:
    """交易所网关 — 封装所有 Binance Futures REST API 调用。"""

    def __init__(self, use_testnet: bool = False):
        cfg = get_exchange_config()
        self._fapi_v1 = cfg.fapi_v1_base
        self._fapi_v2 = cfg.fapi_v2_base
        self._sapi_v1 = cfg.sapi_v1_base
        self._api_key = cfg.api_key
        self._api_secret = cfg.api_secret
        self._proxy = {"http": cfg.proxy, "https": cfg.proxy} if cfg.proxy else {}
        self._timeout = cfg.request_timeout_seconds
        self._recv_window = cfg.recv_window_ms
        self._session = req.Session()

    # ─── 工具方法 ────────────────────────────────

    def _ts(self) -> int:
        return int(time.time() * 1000)

    def _request_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(sorted(params.items()))
        return hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _call(self, method: str, base: str, path: str, params: dict,
              request_id: str, symbol: str) -> dict:
        """统一 REST 调用（签名版）。"""
        p = dict(params)
        p["timestamp"] = self._ts()
        p["recvWindow"] = self._recv_window
        q = urllib.parse.urlencode(sorted(p.items()))
        sig = self._sign(p)
        url = f"{base}/{path}?{q}&signature={sig}"

        hdrs = {"X-MBX-APIKEY": self._api_key}
        t0 = time.monotonic()

        try:
            if method == "GET":
                resp = self._session.get(url, headers=hdrs, timeout=self._timeout, proxies=self._proxy)
            elif method == "POST":
                resp = self._session.post(url, headers=hdrs, timeout=self._timeout, proxies=self._proxy)
            elif method == "DELETE":
                resp = self._session.delete(url, headers=hdrs, timeout=self._timeout, proxies=self._proxy)
            else:
                raise ValueError(f"Unknown method: {method}")
        except req.Timeout:
            latency = (time.monotonic() - t0) * 1000
            logger.error("request_id=%s symbol=%s op=%s/%s latency=%.0fms TIMEOUT",
                         request_id, symbol, method, path, latency)
            raise ExchangeTimeoutError(
                f"{method} {path} timed out", request_id=request_id, symbol=symbol, latency_ms=latency)
        except req.ConnectionError as e:
            latency = (time.monotonic() - t0) * 1000
            logger.error("request_id=%s symbol=%s op=%s/%s latency=%.0fms CONN_ERR: %s",
                         request_id, symbol, method, path, latency, e)
            raise ExchangeTimeoutError(
                f"Connection error: {e}", request_id=request_id, symbol=symbol, latency_ms=latency)

        latency = (time.monotonic() - t0) * 1000

        if resp.status_code != 200:
            exc = map_binance_error(resp, request_id=request_id, symbol=symbol, latency_ms=latency)
            logger.error("request_id=%s symbol=%s op=%s/%s http=%d latency=%.0fms err=%s",
                         request_id, symbol, method, path, resp.status_code, latency, exc)
            raise exc

        logger.info("request_id=%s symbol=%s op=%s/%s http=200 latency=%.0fms",
                    request_id, symbol, method, path, latency)
        return resp.json()

    def _call_public(self, base: str, path: str, params: dict = None) -> dict:
        """无需签名的公开 API 调用。"""
        url = f"{base}/{path}"
        resp = self._session.get(url, params=params, timeout=self._timeout, proxies=self._proxy)
        if resp.status_code != 200:
            raise ExchangeError(f"Public API {path} returned {resp.status_code}")
        return resp.json()

    # ─── 仓位查询 ────────────────────────────────

    def get_positions(self) -> list[ExchangePosition]:
        """获取所有持仓（v2 positionRisk）。"""
        rid = self._request_id()
        data = self._call("GET", self._fapi_v2, "positionRisk", {}, rid, "")
        return [map_binance_position(p) for p in data if abs(float(p.get("positionAmt", 0))) > 0]

    def get_open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        """获取未完成订单。"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        rid = self._request_id()
        data = self._call("GET", self._fapi_v1, "openOrders", params, rid, symbol or "")
        return [map_binance_order(o, o.get("symbol", symbol or "")) for o in data]

    def get_algo_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        """获取未完成条件单。无 symbol 时用 openAlgoOrders（可选参数）。"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        rid = self._request_id()
        path = "allAlgoOrders" if symbol else "openAlgoOrders"
        data = self._call("GET", self._fapi_v1, path, params, rid, symbol or "")
        return [map_binance_order(o, o.get("symbol", symbol or "")) for o in data]

    def get_order(self, symbol: str, order_id: str) -> ExchangeOrder:
        """查询单个订单状态。"""
        rid = self._request_id()
        data = self._call("GET", self._fapi_v1, "order", {
            "symbol": symbol,
            "orderId": order_id,
        }, rid, symbol)
        return map_binance_order(data, symbol)

    def get_price(self, symbol: str) -> float:
        """获取最新价格（公开API，无需签名）。"""
        data = self._call_public(self._fapi_v1, "ticker/price", {"symbol": symbol})
        return float(data["price"])

    # ─── 开仓 ────────────────────────────────────

    def place_entry_order(self, request: EntryOrderRequest) -> OrderResult:
        """提交开仓订单（MARKET/LIMIT）。

        自动设置 positionSide、reduceOnly=false。
        """
        rid = self._request_id()
        side = "BUY" if request.side.value == "LONG" else "SELL"

        params = {
            "symbol": request.symbol,
            "side": side,
            "positionSide": request.side.value,
            "type": request.order_type.value,
            "quantity": str(request.quantity),
            "reduceOnly": "false",
            "newClientOrderId": request.client_order_id,
        }
        if request.order_type.value == "LIMIT" and request.price is not None:
            params["price"] = str(request.price)
            params["timeInForce"] = "GTC"

        data = self._call("POST", self._fapi_v1, "order", params, rid, request.symbol)
        logger.info("ENTRY %s %s qty=%s cid=%s orderId=%s",
                    request.symbol, request.side.value, request.quantity,
                    request.client_order_id, data.get("orderId"))
        return map_order_result(data)

    # ─── 平仓 ────────────────────────────────────

    def place_exit_order(self, request: ExitOrderRequest) -> OrderResult:
        """提交平仓订单。

        自动设置 reduceOnly=true, positionSide。
        """
        rid = self._request_id()
        side = "SELL" if request.side.value == "LONG" else "BUY"

        params = {
            "symbol": request.symbol,
            "side": side,
            "positionSide": request.side.value,
            "type": request.order_type.value,
            "quantity": str(request.quantity),
            "reduceOnly": "true",
            "newClientOrderId": request.client_order_id,
        }

        data = self._call("POST", self._fapi_v1, "order", params, rid, request.symbol)
        logger.info("EXIT %s %s qty=%s cid=%s orderId=%s",
                    request.symbol, request.side.value, request.quantity,
                    request.client_order_id, data.get("orderId"))
        return map_order_result(data)

    # ─── 保护单（条件单）──────────────────────────

    def place_protection_order(self, request: ProtectionOrderRequest) -> OrderResult:
        """提交保护单（止损/止盈 Algo Order）。

        使用 FAPI /algoOrder 端点，基于标记价格触发。
        """
        rid = self._request_id()
        close_side = "SELL" if request.position_side.value == "LONG" else "BUY"

        if request.role.value == "STOP_LOSS":
            order_type = "STOP_MARKET"
        elif request.role.value == "TAKE_PROFIT":
            order_type = "TAKE_PROFIT_MARKET"
        elif request.role.value == "TRAILING_STOP":
            order_type = "TRAILING_STOP_MARKET"
        else:
            raise ValueError(f"Unsupported protection role: {request.role}")

        params = {
            "symbol": request.symbol,
            "side": close_side,
            "positionSide": request.position_side.value,
            "type": order_type,
            "triggerprice": str(request.trigger_price),
            "workingType": request.working_type.value,
            "reduceOnly": "true",
            "algotype": "CONDITIONAL",
            "newClientOrderId": request.client_order_id,
        }
        if request.quantity is not None:
            params["quantity"] = str(request.quantity)
        if request.close_position:
            params["closePosition"] = "true"
        if request.price_protect:
            params["priceProtect"] = "true"

        data = self._call("POST", self._fapi_v1, "algoOrder", params, rid, request.symbol)
        logger.info("PROTECTION %s %s role=%s trigger=%s cid=%s algoId=%s",
                    request.symbol, request.position_side.value, request.role.value,
                    request.trigger_price, request.client_order_id, data.get("algoId"))
        return map_order_result(data)

    # ─── 撤单 ────────────────────────────────────

    def cancel_order(self, symbol: str, order_id: str) -> CancelResult:
        """撤销普通订单。"""
        rid = self._request_id()
        try:
            data = self._call("DELETE", self._fapi_v1, "order", {
                "symbol": symbol,
                "orderId": order_id,
            }, rid, symbol)
            logger.info("CANCEL %s orderId=%s OK", symbol, order_id)
            return CancelResult(symbol=symbol, order_id=order_id,
                              client_order_id=data.get("clientOrderId", ""), success=True)
        except ExchangeError as e:
            if "UNKNOWN_ORDER" in str(e) or "does not exist" in str(e).lower():
                return CancelResult(symbol=symbol, order_id=order_id,
                                  client_order_id="", success=True, reason="already_absent")
            return CancelResult(symbol=symbol, order_id=order_id,
                              client_order_id="", success=False, reason=str(e))

    def cancel_algo_order(self, symbol: str, algo_id: str) -> CancelResult:
        """撤销条件单（algo order）。"""
        rid = self._request_id()
        try:
            data = self._call("DELETE", self._fapi_v1, "algoOrder", {
                "symbol": symbol,
                "algoId": algo_id,
            }, rid, symbol)
            logger.info("CANCEL_ALGO %s algoId=%s OK", symbol, algo_id)
            return CancelResult(symbol=symbol, order_id=algo_id,
                              client_order_id=data.get("clientAlgoId", ""), success=True)
        except ExchangeError as e:
            if "UNKNOWN_ORDER" in str(e) or "does not exist" in str(e).lower():
                return CancelResult(symbol=symbol, order_id=algo_id,
                                  client_order_id="", success=True, reason="already_absent")
            return CancelResult(symbol=symbol, order_id=algo_id,
                              client_order_id="", success=False, reason=str(e))


# ─── 全局单例 ────────────────────────────────────

_gateway: Optional[ExchangeGateway] = None


def get_gateway() -> ExchangeGateway:
    """获取全局 ExchangeGateway 单例。"""
    global _gateway
    if _gateway is None:
        _gateway = ExchangeGateway()
    return _gateway

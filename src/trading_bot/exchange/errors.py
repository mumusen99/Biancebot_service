"""
统一交易所异常层次结构。
所有 Binance API 错误映射为内部异常，策略层不得直接处理 HTTP 状态码。
"""


class ExchangeError(Exception):
    """交易所基础异常。"""
    def __init__(self, message: str, request_id: str = "", symbol: str = "",
                 exchange_code: int = 0, latency_ms: float = 0.0):
        super().__init__(message)
        self.request_id = request_id
        self.symbol = symbol
        self.exchange_code = exchange_code
        self.latency_ms = latency_ms


class ExchangeTimeoutError(ExchangeError):
    """API 请求超时。"""
    pass


class ExchangeRateLimitError(ExchangeError):
    """触发交易所限频。"""
    pass


class ExchangeAuthError(ExchangeError):
    """API 鉴权失败（密钥无效/过期/权限不足）。"""
    pass


class ExchangePrecisionError(ExchangeError):
    """数量/价格精度不符合交易所要求。"""
    pass


class ExchangeInsufficientMarginError(ExchangeError):
    """保证金不足。"""
    pass


class ExchangeOrderRejectedError(ExchangeError):
    """订单被交易所拒绝（非精度/保证金原因）。"""
    pass


class ExchangeStateConflictError(ExchangeError):
    """订单状态与预期不符（如订单已不存在、重复等）。"""
    pass


class ExchangeOrderNotFoundError(ExchangeStateConflictError):
    """订单不存在。"""
    pass


class ExchangeNetworkError(ExchangeError):
    """网络连接错误。"""
    pass


# HTTP 状态码 → 异常映射
HTTP_ERROR_MAP = {
    401: ExchangeAuthError,
    403: ExchangeAuthError,
    429: ExchangeRateLimitError,
    418: ExchangeRateLimitError,  # Binance IP ban
}


def map_binance_error(response, request_id: str = "", symbol: str = "",
                      latency_ms: float = 0.0) -> ExchangeError:
    """将 Binance HTTP 响应映射为内部异常。"""
    status = response.status_code if hasattr(response, 'status_code') else 0
    try:
        body = response.json() if hasattr(response, 'json') else {}
    except Exception:
        body = {}
    msg = body.get('msg', str(response))

    exc_cls = HTTP_ERROR_MAP.get(status, ExchangeError)

    # Binance 特定错误码映射
    code = body.get('code', 0)
    if code == -2010:  # 保证金不足
        exc_cls = ExchangeInsufficientMarginError
    elif code == -2011:  # 订单被拒绝
        exc_cls = ExchangeOrderRejectedError
    elif code == -2013:  # 订单不存在
        exc_cls = ExchangeOrderNotFoundError
    elif code == -2021:  # 立即取消
        exc_cls = ExchangeOrderRejectedError
    elif code == -4061:  # 订单状态冲突
        exc_cls = ExchangeStateConflictError

    return exc_cls(
        message=f"[{code}] {msg}",
        request_id=request_id,
        symbol=symbol,
        exchange_code=code,
        latency_ms=latency_ms,
    )

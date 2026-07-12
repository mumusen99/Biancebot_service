"""
统一环境配置 — 所有模块的唯一交换环境事实来源。

原则：
- 环境变量仅用于：API Key、API Secret、环境选择、配置/日志目录
- 禁止模块中再出现硬编码的 Binance URL
- 禁止多个布尔开关组合判断环境
- 配置惰性初始化：仅在调用 get_exchange_config() 时验证环境变量
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Optional


class TradingEnvironment(StrEnum):
    """统一环境枚举。只允许 testnet 或 live。"""
    TESTNET = "testnet"
    LIVE = "live"


# Binance Futures API 端点
_LIVE_REST_BASE = "https://fapi.binance.com"
_LIVE_WS_BASE = "wss://fstream.binance.com/ws"
_LIVE_WS_STREAM = "wss://fstream.binance.com/stream"

_TESTNET_REST_BASE = "https://testnet.binancefuture.com"
_TESTNET_WS_BASE = "wss://stream.binancefuture.com/ws"
_TESTNET_WS_STREAM = "wss://stream.binancefuture.com/stream"


class EnvironmentError(Exception):
    """环境配置错误。"""
    pass


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    """不可变交换环境配置。启动时构建，全局唯一。"""
    environment: TradingEnvironment
    rest_base_url: str       # e.g. https://fapi.binance.com
    fapi_v1_base: str        # e.g. https://fapi.binance.com/fapi/v1
    fapi_v2_base: str        # e.g. https://fapi.binance.com/fapi/v2
    sapi_v1_base: str        # e.g. https://api.binance.com/sapi/v1
    ws_base_url: str         # e.g. wss://fstream.binance.com/ws
    ws_stream_url: str       # e.g. wss://fstream.binance.com/stream
    api_key: str
    api_secret: str
    recv_window_ms: int = 10000
    request_timeout_seconds: float = 15.0
    proxy: str = ""


def _resolve_environment() -> TradingEnvironment:
    """从单一环境变量 TRADING_ENV 解析环境。抛出 EnvironmentError 而非直接 exit。"""
    env_val = os.getenv("TRADING_ENV", "").strip().lower()
    if not env_val:
        raise EnvironmentError(
            "TRADING_ENV is not set. Set TRADING_ENV=testnet or TRADING_ENV=live."
        )

    if env_val == "testnet":
        return TradingEnvironment.TESTNET
    elif env_val == "live":
        ack = os.getenv("LIVE_TRADING_ACK", "").strip()
        if ack != "I_UNDERSTAND_THIS_SENDS_REAL_ORDERS":
            raise EnvironmentError(
                "TRADING_ENV=live requires LIVE_TRADING_ACK=I_UNDERSTAND_THIS_SENDS_REAL_ORDERS"
            )
        return TradingEnvironment.LIVE
    else:
        raise EnvironmentError(
            f"Unknown TRADING_ENV='{env_val}'. Must be 'testnet' or 'live'."
        )


@lru_cache(maxsize=1)
def get_exchange_config() -> ExchangeConfig:
    """获取全局唯一交换环境配置（惰性初始化，缓存）。

    仅在调用时验证环境变量，不会在模块 import 时崩溃。
    """
    env = _resolve_environment()

    if env == TradingEnvironment.LIVE:
        rest = _LIVE_REST_BASE
        ws = _LIVE_WS_BASE
        ws_stream = _LIVE_WS_STREAM
    else:
        rest = _TESTNET_REST_BASE
        ws = _TESTNET_WS_BASE
        ws_stream = _TESTNET_WS_STREAM

    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise EnvironmentError(
            "BINANCE_API_KEY and BINANCE_API_SECRET must be set."
        )

    proxy = os.getenv("BINANCE_PROXY", "").strip()
    recv_window = int(os.getenv("BINANCE_RECV_WINDOW", "10000"))
    timeout = float(os.getenv("BINANCE_REQUEST_TIMEOUT", "15.0"))

    return ExchangeConfig(
        environment=env,
        rest_base_url=rest,
        fapi_v1_base=f"{rest}/fapi/v1",
        fapi_v2_base=f"{rest}/fapi/v2",
        sapi_v1_base="https://api.binance.com/sapi/v1",
        ws_base_url=ws,
        ws_stream_url=ws_stream,
        api_key=api_key,
        api_secret=api_secret,
        recv_window_ms=recv_window,
        request_timeout_seconds=timeout,
        proxy=proxy,
    )


def print_startup_info(cfg: ExchangeConfig) -> None:
    """启动日志 — 必须明确打印环境信息（不打印 API Secret）。"""
    print(f"""
╔══════════════════════════════════════════╗
║  TRADING BOT STARTUP                    ║
╠══════════════════════════════════════════╣
║  ENVIRONMENT   = {cfg.environment.value.upper():<15}       ║
║  REST BASE     = {cfg.rest_base_url:<30} ║
║  WS BASE       = {cfg.ws_base_url:<30} ║
║  ACCOUNT MODE  = HEDGE                  ║
║  ORDER WRITES  = {'ENABLED' if cfg.environment == TradingEnvironment.LIVE else 'ENABLED (TESTNET)':<15} ║
║  RECV WINDOW   = {cfg.recv_window_ms}ms                    ║
║  TIMEOUT       = {cfg.request_timeout_seconds}s                     ║
║  PROXY         = {'(none — direct)' if not cfg.proxy else cfg.proxy:<15} ║
╚══════════════════════════════════════════╝
""")


# ─── 便捷函数 — 向后兼容 ───

def is_testnet() -> bool:
    return get_exchange_config().environment == TradingEnvironment.TESTNET

def is_live() -> bool:
    return get_exchange_config().environment == TradingEnvironment.LIVE

def get_fapi_v1() -> str:
    return get_exchange_config().fapi_v1_base

def get_fapi_v2() -> str:
    return get_exchange_config().fapi_v2_base

def get_ws_base() -> str:
    return get_exchange_config().ws_base_url

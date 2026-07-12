# ⚠️ FROZEN: legacy module. Only fatal security fixes allowed. No new features.
"""
数据获取模块
------------
统一用 requests 直调 Binance FAPI，CCXT 只在需要时手工使用。
"""
import json
import time
import threading
import logging
import hashlib
import hmac
import urllib.parse
from datetime import datetime
from typing import Optional
from copy import deepcopy

import pandas as pd
import requests as req

from trading_bot.core.settings import (
    SYMBOLS, API_KEY, API_SECRET, PROXY,
    STATE_FILE, DEFAULT_LEVERAGE, TIMEFRAMES,
)

logger = logging.getLogger("data_fetcher")

# ─── API 端点 ────────────────────────────────────────
_TESTNET = "https://testnet.binancefuture.com/fapi/v1"
_LIVE = "https://fapi.binance.com/fapi/v1"
_API = lambda: _LIVE

# ─── 代理连接修复 ────────────────────────────────────────
# 当前代理节点 (45.78.52.57) 对 Binance CloudFront 的 keepalive 不稳定
# 连接复用时经常 SSL EOF → 每次请求强制新建连接避免
_headers_no_ka = {"Connection": "close"}


class _SharedSession:
    """Persistent HTTP session; disable keepalive only when a proxy is used."""
    def __init__(self):
        self._session = req.Session()
        if PROXY:
            self._session.proxies = {"http": PROXY, "https": PROXY}
            self._session.headers.update(_headers_no_ka)
    def get(self, url, **kwargs): return self._session.get(url, **kwargs)
    def post(self, url, **kwargs): return self._session.post(url, **kwargs)
    def delete(self, url, **kwargs): return self._session.delete(url, **kwargs)

_session = _SharedSession()
_CACHE_LOCK = threading.RLock()
_CACHE = {}

def _cached(key, ttl, loader):
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] <= ttl:
            value = hit[1]
            return deepcopy(value)
    value = loader()
    with _CACHE_LOCK: _CACHE[key] = (now, value)
    return deepcopy(value)

def _ts() -> int:
    return int(time.time() * 1000)


def _sign_url(base: str, path: str, params: dict) -> str:
    """构建已签名的完整 URL (避免 requests params= 编码不一致)"""
    q = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode("utf-8"), q.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{base}/{path}?{q}&signature={sig}"


def _fapi_get(path: str, params: dict = None, signed: bool = False, api_version: str = None,
              _retry: int = 3):
    """GET 请求 Binance FAPI（自动重试 SSL EOF，最多3次+退避）"""
    p = dict(params or {})
    base = _API()
    if api_version:
        base = base.replace("fapi/v1", f"fapi/{api_version}")
    for attempt in range(1, _retry + 1):
        t0 = time.time()
        try:
            if signed:
                p["timestamp"] = _ts()
                p["recvWindow"] = 10000
                url = _sign_url(base, path, p)
                resp = _session.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=5)
            else:
                resp = _session.get(f"{base}/{path}", params=p, timeout=5)
            elapsed = time.time() - t0
            if resp.status_code != 200:
                raise Exception(f"API {resp.status_code}: {resp.text[:300]}")
            # 记录慢请求(>4s)和正常请求
            if elapsed > 4:
                logger.warning(f"  ⌛ 慢请求 {path[:40]} 耗时{elapsed:.1f}s status={resp.status_code}")
            elif elapsed > 2:
                logger.info(f"  ⏱ {path[:40]} {elapsed:.1f}s")
            return resp.json()
        except Exception as e:
            elapsed = time.time() - t0
            err_str = str(e)
            err_type = type(e).__name__
            # 记录超时/异常的耗时 + 原因
            if "Read timed out" in err_str:
                logger.warning(f"  ⏰ 超时 {path[:40]} 耗时{elapsed:.1f}s (read timeout=5s)")
            elif "Connection refused" in err_str:
                logger.warning(f"  🔌 连接拒绝 {path[:40]} 耗时{elapsed:.1f}s")
            elif "ProxyError" in err_str:
                logger.warning(f"  🌐 代理异常 {path[:40]} 耗时{elapsed:.1f}s")
            elif "451" in err_str:
                logger.warning(f"  🚫 geo封锁 {path[:40]} status=451")
            elif "SSL" in err_str or "EOF" in err_str or "reset" in err_str:
                if attempt < _retry:
                    delay = attempt * 1.0
                    logger.warning(f"  ⚡ {err_type} {path[:40]} 重试{attempt}/{_retry} (等{delay}s) 耗时{elapsed:.1f}s")
                    time.sleep(delay)
                    continue
            else:
                logger.warning(f"  ❓ {err_type} {path[:40]} 耗时{elapsed:.1f}s: {err_str[:100]}")
            raise


def get_exchange():
    """统一返回 None（全部用 requests 直连）"""
    mode = "💰 实盘"
    logger.info(f"{mode} 模式 (requests 直连)")
    return None


# ─────────────────────── 公开 API ───────────────────────


def fetch_all_tickers(exchange=None) -> dict:
    """批量获取所有 USDT 合约 24h ticker（2秒共享缓存）"""
    return _cached(("all_tickers",), 2.0, _fetch_all_tickers_uncached)

def _fetch_all_tickers_uncached() -> dict:
    try:
        raw = _fapi_get("ticker/24hr")
        return {
            t["symbol"]: {
                "symbol": t["symbol"],
                "last": float(t.get("lastPrice", 0)),
                "change24h": float(t.get("priceChangePercent", 0)),
                "volume24h": float(t.get("quoteVolume", 0)),
                "high24h": float(t.get("highPrice", 0)),
                "low24h": float(t.get("lowPrice", 0)),
            }
            for t in raw if t.get("symbol", "").endswith("USDT")
        }
    except Exception as e:
        logger.error(f"批量获取 tickers 失败: {e}")
        return {}


def fetch_klines(exchange=None, symbol="BTCUSDT", timeframe="5m", limit=200) -> pd.DataFrame:
    """获取 K 线数据；同一进程内按周期缓存，减少重复请求。"""
    ttl = 5.0 if timeframe == "1m" else 15.0 if timeframe == "5m" else 30.0
    return _cached(("klines", symbol, timeframe, int(limit)), ttl, lambda: _fetch_klines_uncached(symbol, timeframe, limit))

def _fetch_klines_uncached(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    try:
        raw = _fapi_get("klines", {
            "symbol": symbol,
            "interval": timeframe,
            "limit": limit,
        })
        data = [[k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in raw]
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df["datetime"] = df["timestamp"].dt.tz_localize("UTC").dt.tz_convert("Asia/Shanghai")
        return df
    except Exception as e:
        logger.error(f"获取 K线 失败 {symbol} {timeframe}: {e}")
        return pd.DataFrame()


def fetch_ticker(exchange=None, symbol="BTCUSDT") -> Optional[dict]:
    """获取 ticker（1秒共享缓存）"""
    return _cached(("ticker", symbol), 1.0, lambda: _fetch_ticker_uncached(symbol))

def _fetch_ticker_uncached(symbol: str) -> Optional[dict]:
    try:
        raw = _fapi_get("ticker/24hr", {"symbol": symbol})
        return {
            "symbol": symbol,
            "last": float(raw.get("lastPrice", 0)),
            "bid": float(raw.get("bidPrice", 0)),
            "ask": float(raw.get("askPrice", 0)),
            "high24h": float(raw.get("highPrice", 0)),
            "low24h": float(raw.get("lowPrice", 0)),
            "change24h": float(raw.get("priceChangePercent", 0)),
            "volume24h": float(raw.get("quoteVolume", 0)),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        logger.error(f"获取 ticker 失败 {symbol}: {e}")
        return None


def fetch_orderbook(exchange=None, symbol="BTCUSDT", limit=10) -> Optional[dict]:
    """获取深度"""
    try:
        raw = _fapi_get("depth", {"symbol": symbol, "limit": limit})
        bids = [[float(p), float(q)] for p, q in raw.get("bids", [])[:5]]
        asks = [[float(p), float(q)] for p, q in raw.get("asks", [])[:5]]
        spread = asks[0][0] - bids[0][0] if asks and bids else None
        return {"bids": bids, "asks": asks, "spread": spread}
    except Exception as e:
        logger.error(f"获取深度失败 {symbol}: {e}")
        return None


def fetch_balance(exchange=None) -> dict:
    """获取 USDT 合约余额"""
    try:
        # 用 fapi/v2 避免 404 问题
        acct = _fapi_get("account", {}, signed=True, api_version="v2")
        return {
            "total": round(float(acct.get("totalMarginBalance", 0)), 2),
            "free": round(float(acct.get("availableBalance", 0)), 2),
            "used": round(float(acct.get("totalInitialMargin", 0)), 2),
        }
    except Exception as e:
        logger.error(f"获取余额失败: {e}")
        return {"total": 0, "free": 0, "used": 0}


def fetch_positions(exchange=None) -> list:
    """获取持仓"""
    try:
        raw = _fapi_get("positionRisk", {}, signed=True, api_version="v2")
        active = []
        if isinstance(raw, list):
            for p in raw:
                amt = float(p.get("positionAmt", 0) or 0)
                if abs(amt) < 0.001:
                    continue
                entry = float(p.get("entryPrice", 0) or 0)
                mark = float(p.get("markPrice", 0) or 0)
                upnl = float(p.get("unRealizedProfit", 0) or 0)
                lev = int(p.get("leverage", 1))
                notional = entry * abs(amt) / lev
                pnl_pct = round(upnl / notional * 100, 2) if notional > 0 else 0
                active.append({
                    "symbol": p["symbol"],
                    "side": "LONG" if amt > 0 else "SHORT",
                    "size": abs(amt),
                    "entry_price": entry,
                    "mark_price": mark,
                    "pnl": round(upnl, 2),
                    "pnl_percent": pnl_pct,
                    "leverage": lev,
                })
        return active
    except Exception as e:
        logger.error(f"获取持仓失败: {e}")
        return []


def get_all_market_data(exchange=None, symbol="BTCUSDT") -> dict:
    """聚合市场数据"""
    data = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "ticker": fetch_ticker(None, symbol),
        "orderbook": fetch_orderbook(None, symbol),
        "klines": {},
    }
    for tf in TIMEFRAMES:
        df = fetch_klines(None, symbol, tf, limit=100)
        if not df.empty:
            data["klines"][tf] = df.to_dict(orient="records")
    return data


# ─── 模拟持仓/余额 (测试网) ─────────────────────────


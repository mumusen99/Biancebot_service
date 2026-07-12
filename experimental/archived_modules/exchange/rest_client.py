"""统一 REST 客户端。所有新模块通过这里访问 Binance API。"""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Optional

from trading_bot.core.settings import FAPI_BASE, PROXY, API_KEY, API_SECRET

logger = logging.getLogger(__name__)

# ─── 配置 ───
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class RestClient:
    """统一 REST 客户端，带签名、重试、限速"""

    def __init__(self, base_url: str = None, api_key: str = None, api_secret: str = None):
        self.base_url = (base_url or FAPI_BASE).rstrip('/')
        self.api_key = api_key or API_KEY
        self.api_secret = api_secret or API_SECRET
        self._proxies = {'http': PROXY, 'https': PROXY} if PROXY else None

    def _signed_request(self, method: str, path: str, params: dict = None,
                        signed: bool = False) -> dict:
        """发送签名的 REST 请求，含重试"""
        params = params or {}
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            qs = urllib.parse.urlencode(params)
            sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            url = f'{self.base_url}{path}?{qs}&signature={sig}'
        else:
            qs = urllib.parse.urlencode(params) if params else ''
            url = f'{self.base_url}{path}'
            if qs:
                url += f'?{qs}'

        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                req = urllib.request.Request(url, method=method)
                if signed:
                    req.add_header('X-MBX-APIKEY', self.api_key)
                with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
                    data = resp.read().decode()
                    result = json.loads(data) if data else {}
                    if isinstance(result, dict) and result.get('code'):
                        logger.warning(f'API error {result.get("code")}: {result.get("msg")}')
                    return result
            except urllib.error.HTTPError as e:
                last_exc = e
                body = e.read().decode()[:200] if hasattr(e, 'read') else str(e)
                logger.error(f'HTTP {e.code} {method} {path}: {body}')
                if e.code == 429:
                    time.sleep(RETRY_DELAY * (attempt + 1))
            except Exception as e:
                last_exc = e
                logger.warning(f'attempt {attempt+1}/{MAX_RETRIES} failed: {e}')
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
        raise last_exc or RuntimeError(f'{method} {path} failed after {MAX_RETRIES} retries')

    def get(self, path: str, params: dict = None, signed: bool = False) -> dict:
        return self._signed_request('GET', path, params, signed)

    def post(self, path: str, params: dict = None) -> dict:
        return self._signed_request('POST', path, params, signed=True)

    def delete(self, path: str, params: dict = None) -> dict:
        return self._signed_request('DELETE', path, params, signed=True)

    # ─── 便捷方法 ───

    def ping(self) -> bool:
        try:
            self.get('/fapi/v1/ping')
            return True
        except Exception:
            return False

    def get_exchange_info(self) -> dict:
        return self.get('/fapi/v1/exchangeInfo')

    def get_ticker_24hr(self, symbol: str = None) -> dict | list:
        params = {'symbol': symbol} if symbol else None
        result = self.get('/fapi/v1/ticker/24hr', params)
        return result

    def get_klines(self, symbol: str, interval: str = '1m', limit: int = 100,
                   start_time: int = None, end_time: int = None) -> list:
        params = {'symbol': symbol.upper(), 'interval': interval, 'limit': limit}
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        result = self.get('/fapi/v1/klines', params)
        return [{'t': k[0], 'o': float(k[1]), 'h': float(k[2]),
                  'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5]),
                  'T': k[6]} for k in result] if isinstance(result, list) else []

    def get_position_risk(self, symbol: str = None) -> list:
        result = self.get('/fapi/v2/positionRisk', {}, signed=True)
        positions = result if isinstance(result, list) else []
        if symbol:
            positions = [p for p in positions if p['symbol'] == symbol.upper()]
        return positions

    def get_open_orders(self, symbol: str = None) -> list:
        params = {'symbol': symbol.upper()} if symbol else {}
        return self.get('/fapi/v1/openOrders', params, signed=True)

    def get_account(self) -> dict:
        return self.get('/fapi/v2/account', {}, signed=True)

    def place_order(self, symbol: str, side: str, order_type: str, quantity: str,
                    price: str = None, stop_price: str = None,
                    reduce_only: str = None, time_in_force: str = 'GTC',
                    client_order_id: str = None) -> dict:
        params = {
            'symbol': symbol.upper(), 'side': side.upper(), 'type': order_type.upper(),
            'quantity': quantity,
        }
        if price:
            params['price'] = price
        if stop_price:
            params['stopPrice'] = stop_price
        if reduce_only:
            params['reduceOnly'] = reduce_only
        if time_in_force and order_type.upper() != 'MARKET':
            params['timeInForce'] = time_in_force
        if client_order_id:
            params['newClientOrderId'] = client_order_id
        return self.post('/fapi/v1/order', params)

    def cancel_order(self, symbol: str, order_id: str = None,
                     client_order_id: str = None) -> dict:
        params = {'symbol': symbol.upper()}
        if order_id:
            params['orderId'] = order_id
        if client_order_id:
            params['origClientOrderId'] = client_order_id
        return self.delete('/fapi/v1/order', params)

    def cancel_all_orders(self, symbol: str) -> dict:
        return self.delete('/fapi/v1/allOpenOrders', {'symbol': symbol.upper()})

    def get_order_book(self, symbol: str, limit: int = 5) -> dict:
        return self.get('/fapi/v1/depth', {'symbol': symbol.upper(), 'limit': limit})


# 全局实例
rest = RestClient()

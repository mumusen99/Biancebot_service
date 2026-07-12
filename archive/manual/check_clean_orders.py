"""
检查并清理重复委托
每次心跳先跑这个，再跑 auto_trader
用直接 API 全量拉取（不依赖 trader.get_open_orders 的币种限制）
"""
import time

# 纯实盘模式
IS_TESTNET = False
import hmac
import hashlib
import urllib.parse
import logging

import requests as req
from trader import IS_TESTNET, TESTNET_FAPI, LIVE_FAPI
from config import API_KEY, API_SECRET, PROXY

logging.basicConfig(level=logging.INFO, format="%(asctime)s [clean] %(message)s")
logger = logging.getLogger("clean_orders")

BASE = TESTNET_FAPI if False else LIVE_FAPI


def _signed_get(path: str, params: dict = None) -> list:
    """GET 请求签名版"""
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 10000
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}/{path}?{q}&signature={sig}"
    hdrs = {"X-MBX-APIKEY": API_KEY}
    prox = {"http": PROXY, "https": PROXY}
    resp = req.get(url, headers=hdrs, timeout=15, proxies=prox)
    if resp.status_code != 200:
        raise Exception(f"GET {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _signed_del(path: str, params: dict = None) -> dict:
    """DELETE 请求签名版"""
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 10000
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}/{path}?{q}&signature={sig}"
    hdrs = {"X-MBX-APIKEY": API_KEY}
    prox = {"http": PROXY, "https": PROXY}
    resp = req.delete(url, headers=hdrs, timeout=15, proxies=prox)
    if resp.status_code != 200:
        raise Exception(f"DEL {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def main():
    # 全量拉取所有开放挂单
    orders = _signed_get("openOrders")
    logger.info(f"当前挂单数: {len(orders)}")

    # 按 symbol + (side, positionSide) 分组
    groups: dict = {}
    for o in orders:
        key = (o["symbol"], o["side"], o["positionSide"])
        groups.setdefault(key, []).append(o)

    total_canceled = 0
    for (sym, side, pos_side), same_side in groups.items():
        if len(same_side) <= 1:
            continue
        logger.warning(f"⚠️ 重复委托: {sym} {side}-{pos_side} 共 {len(same_side)} 个")
        # 保留最新的（time 最大），取消其他的
        same_side.sort(key=lambda x: x["time"], reverse=True)
        for dup in same_side[1:]:
            oid = dup["orderId"]
            try:
                _signed_del("order", {"symbol": sym, "orderId": oid})
                logger.info(f"   ✅ 已取消: ID {oid}")
                total_canceled += 1
            except Exception as e:
                logger.error(f"   ❌ 取消失败 ID {oid}: {e}")

    if total_canceled:
        print(f"✅ 共取消 {total_canceled} 个重复委托")
    else:
        print("✅ 无重复委托")


if __name__ == "__main__":
    main()

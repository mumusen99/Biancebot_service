"""
订单执行模块
------------
开仓、平仓、设置止盈止损、仓位管理。
统一用 requests 直连 Binance FAPI (测试网和实盘均可)。
"""
import json
import logging
import time
import hmac
import hashlib
import urllib.parse
from datetime import datetime
import math
from typing import Optional

import requests as req

from trading_bot.core.settings import (
    API_KEY, API_SECRET, PROXY, SYMBOLS, DEFAULT_LEVERAGE,
    MAX_POSITION_USDT, STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT,
    STATE_FILE,
)
from trading_bot.core.env_config import get_exchange_config, is_testnet as _is_testnet

# 向后兼容：IS_TESTNET 来源于统一环境配置
IS_TESTNET = _is_testnet()

# ─── 全局精度缓存 ───────────────────────────────────
_SYMBOL_PRECISIONS: dict = {}  # symbol -> (quantity_decimals, step_size, price_decimals, tick_size)

def _load_precisions(exchange=None):
    """动态加载所有交易对精度"""
    global _SYMBOL_PRECISIONS
    if _SYMBOL_PRECISIONS and not exchange:
        return _SYMBOL_PRECISIONS
    try:
        base = get_exchange_config().fapi_v1_base
        proxy = {"http": PROXY, "https": PROXY}
        headers = {"X-MBX-APIKEY": API_KEY}
        resp = req.get(f"{base}/exchangeInfo", headers=headers, timeout=15, proxies=proxy)
        data = resp.json()
        for s in data.get("symbols", []):
            sym = s["symbol"]
            qty_dec, step = 8, 0.00000001
            price_dec, tick = 8, 0.00000001
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                    qty_dec = max(0, -int(f["stepSize"].find("1") - len(f["stepSize"]) + 1) if "1" in f["stepSize"] else 8)
                    # Actually compute decimal places from stepSize string
                    ss = f["stepSize"].rstrip("0")
                    if "." in ss:
                        qty_dec = len(ss.split(".")[1])
                    else:
                        qty_dec = 0
                if f["filterType"] == "PRICE_FILTER":
                    tick = float(f["tickSize"])
                    ts = f["tickSize"].rstrip("0")
                    if "." in ts:
                        price_dec = len(ts.split(".")[1])
                    else:
                        price_dec = 0
            _SYMBOL_PRECISIONS[sym] = (qty_dec, step, price_dec, tick)
    except Exception as e:
        logger.warning(f"加载精度信息失败，回退到硬编码: {e}")
    return _SYMBOL_PRECISIONS

def _get_symbol_precision(symbol: str) -> tuple:
    """获取交易对精度，优先动态，回退硬编码"""
    if symbol in _SYMBOL_PRECISIONS:
        return _SYMBOL_PRECISIONS[symbol]
    # 硬编码回退
    fallback = {
        "BTCUSDT": (3, 0.001, 1, 0.1),
        "ETHUSDT": (3, 0.001, 2, 0.01),
        "SOLUSDT": (2, 0.01, 2, 0.01),
        "BNBUSDT": (2, 0.01, 2, 0.01),
        "XRPUSDT": (1, 0.1, 4, 0.0001),
        "ADAUSDT": (1, 0.1, 4, 0.0001),
        "DOGEUSDT": (0, 1, 5, 0.00001),
        "DOTUSDT": (2, 0.01, 3, 0.001),
        "AVAXUSDT": (2, 0.01, 3, 0.001),
        "LINKUSDT": (2, 0.01, 3, 0.001),
        "NEARUSDT": (2, 0.01, 3, 0.001),
        "APTUSDT": (1, 0.1, 3, 0.001),
        "ARBUSDT": (1, 0.1, 3, 0.001),
        "OPUSDT": (1, 0.1, 4, 0.0001),
        "MATICUSDT": (1, 0.1, 4, 0.0001),
        "PEPEUSDT": (0, 1, 8, 0.00000001),
        "WLDUSDT": (0, 1, 7, 0.0001),
        "HYPEUSDT": (1, 0.1, 3, 0.001),
        "XAUUSDT": (3, 0.001, 2, 0.01),
        "XAGUSDT": (2, 0.01, 3, 0.001),
        "SNDKUSDT": (2, 0.01, 3, 0.001),
        "MUUSDT": (2, 0.01, 3, 0.001),
        "SOXLUSDT": (2, 0.01, 3, 0.001),
        "SKHYNIXUSDT": (2, 0.01, 3, 0.001),
    }
    return fallback.get(symbol, (0, 1, 7, 0.0001))


def _tick_decimals(tick: float) -> int:
    """Determine decimal places needed for a tick size, handling scientific notation"""
    if tick <= 0:
        return 8
    # 对于 0.00001 这样的数，str() 会变成 "1e-05"
    # 用 format 确保完整显示
    s = f"{tick:.10f}"  # "0.0000100000"
    # 去掉尾部多余的0
    s = s.rstrip('0')
    if '.' in s:
        return len(s.split('.')[1])
    return 0


def _align_price(symbol: str, price: float) -> float:
    """
    将价格对齐到 tick size。
    防止 -1111 (Precision over max) 错误。
    
    自动处理:
    - float 精度问题（0.1不能精确表示的浮点数）
    - 非常小的 tick（如 DOGE 0.00001，会变成 1e-05）
    - 整数 tick
    - 取整方向（round 到最近）
    """
    if not price or price <= 0:
        return price
    _load_precisions()
    _, _, _, tick = _get_symbol_precision(symbol)
    if not tick or tick <= 0:
        return round(price, 8)
    
    decimals = _tick_decimals(tick)
    multiplier = 10 ** decimals
    
    # 转为整数运算避免浮点误差
    tick_in_units = int(round(tick * multiplier))
    price_in_units = int(price * multiplier + 0.5)
    
    # 对齐到最近的 tick 倍数
    aligned_units = int((price_in_units / tick_in_units) + 0.5) * tick_in_units
    
    return float(f"{{:.{decimals}f}}".format(aligned_units / multiplier))


def _align_sltp(symbol: str, sl_price: float, tp_price: float, side: str) -> tuple:
    """对齐止盈止损价格到 tick size，方向安全化。
    
    LONG:  止损向上取整（更易触发，保护利润）
           止盈向下取整（更易达到，落袋为安）
    SHORT: 止损向下取整（更易触发，保护利润）
           止盈向上取整（更易达到，落袋为安）
    """
    if side == "LONG":
        sl = _align_price_dir(symbol, sl_price, 'up')    # 止损紧一点
        tp = _align_price_dir(symbol, tp_price, 'down')  # 止盈易一点
    else:
        sl = _align_price_dir(symbol, sl_price, 'down')  # 止损紧一点
        tp = _align_price_dir(symbol, tp_price, 'up')    # 止盈易一点
    return sl, tp


def _align_price_dir(symbol: str, price: float, direction: str = 'nearest') -> float:
    """
    按方向对齐 tick size。
    direction='down' → 向下取整（做多止损用：让止损更紧，先触发）
    direction='up'   → 向上取整（做空止损用：让止损更紧，先触发）
    direction='nearest' → 四舍五入（普通计算用）
    """
    if not price or price <= 0:
        return price
    _load_precisions()
    _, _, _, tick = _get_symbol_precision(symbol)
    if not tick or tick <= 0:
        return round(price, 8)
    
    decimals = _tick_decimals(tick)
    multiplier = 10 ** decimals
    tick_in_units = int(round(tick * multiplier))
    price_in_units = int(price * multiplier + 0.5)
    
    # 先用 floor/ceil/round 把原始价格转成整数单位
    if direction == 'down':
        base_units = int(price * multiplier // 1) if price > 0 else 0
    elif direction == 'up':
        base_units = -int(-price * multiplier // 1) if price > 0 else 0
    else:
        base_units = int(round(price * multiplier))
    
    aligned_units = int(base_units / tick_in_units) * tick_in_units
    
    return float(f"{{:.{decimals}f}}".format(aligned_units / multiplier))


def _align_qty(symbol: str, qty: float, round_up: bool = False) -> float:
    """将数量对齐到 step size，防止 -1111"""
    if not qty or qty <= 0:
        return qty
    _load_precisions()
    _, step, _, _ = _get_symbol_precision(symbol)
    if not step or step <= 0:
        return round(qty, 8)
    
    decimals = _tick_decimals(step)
    multiplier = 10 ** decimals
    step_in_units = int(round(step * multiplier))
    # 数量对齐
    if round_up:
        qty_in_units = math.ceil(qty * multiplier)
        aligned = math.ceil(qty_in_units / step_in_units) * step_in_units
    else:
        qty_in_units = int(qty * multiplier)
        aligned = int(qty_in_units / step_in_units) * step_in_units
    
    return float(f"{{:.{decimals}f}}".format(aligned / multiplier))


logger = logging.getLogger("trader")

# FAPI bases 从统一环境配置读取（惰性）
try:
    _LIVE_FAPI = get_exchange_config().fapi_v1_base
except Exception:
    _LIVE_FAPI = "https://fapi.binance.com/fapi/v1"
# 向后兼容别名
TESTNET_FAPI = "https://testnet.binancefuture.com/fapi/v1"
LIVE_FAPI = _LIVE_FAPI


def _ts() -> int:
    return int(time.time() * 1000)


def _sign(params: dict) -> dict:
    query = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    params["signature"] = sig
    return params


def _api(method: str, path: str, params: dict = None, sapi: bool = False) -> dict:
    """调用 Binance API (签名版)"""
    if sapi:
        base = "https://api.binance.com/sapi/v1"
    else:
        base = get_exchange_config().fapi_v1_base
    p = dict(params or {})
    p["timestamp"] = _ts()
    p["recvWindow"] = 10000
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{base}/{path}?{q}&signature={sig}"
    prox = {"http": PROXY, "https": PROXY}
    hdrs = {"X-MBX-APIKEY": API_KEY}
    if method == "GET":
        resp = req.get(url, headers=hdrs, timeout=15, proxies=prox)
    else:
        resp = req.post(url, headers=hdrs, timeout=15, proxies=prox)
    if resp.status_code != 200:
        raise Exception(f"API {method} {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _get_price(symbol: str) -> float:
    """获取最新价"""
    d = _api("GET", "ticker/price", {"symbol": symbol})
    return float(d["price"])


class Trader:
    def __init__(self, paper: Optional[bool] = None):
        self.paper = IS_TESTNET if paper is None else paper
        logger.info("📋 模拟交易模式 (不下真实单)" if self.paper else "💰 实盘交易模式")

    def _adjust_amount(self, symbol: str, amount: float, price: float) -> str:
        """按交易所精度调整数量，返回格式化的字符串"""
        # 最小名义价值 5 USDT — 用 Ceil 确保不下于最小值
        min_value = 5.0
        qty_dec, step, _, _ = _get_symbol_precision(symbol)
        step_int = int(round(step * 10**8))
        
        # 先算最小需要的数量
        min_raw = min_value / price
        # 向上取整到 step 倍数
        min_ceil_int = int(min_raw * 10**8)
        min_steps = (min_ceil_int + step_int - 1) // step_int  # ceil division
        min_amount_int = min_steps * step_int
        
        # user 想要的原始数量
        target_int = int(round(amount * 10**8))
        
        # 取两者中的较大值，并向下对齐到 step
        adjusted_int = max(min_amount_int, int(target_int / step_int) * step_int)
        adjusted = adjusted_int / 10**8
        # 格式化为指定小数位数的字符串
        return f"{adjusted:.{qty_dec}f}"

    def set_leverage(self, symbol: str, leverage: int = DEFAULT_LEVERAGE):
        """设置杠杆"""
        if self.paper:
            logger.info(f"📋 [模拟] 杠杆: {symbol} {leverage}x")
            return
        try:
            # 先获取已设置的杠杆，避免重复设置
            _api("POST", "leverage", {"symbol": symbol, "leverage": leverage})
            logger.info(f"✅ 杠杆设置: {symbol} {leverage}x")
        except Exception as e:
            logger.warning(f"杠杆设置失败: {e}")

    def set_margin_mode(self, symbol: str, isolated: bool = False):
        """设置保证金模式"""
        if self.paper:
            logger.info(f"📋 [模拟] 保证金: {symbol} {'逐仓' if isolated else '全仓'}")
            return
        try:
            mode = "ISOLATED" if isolated else "CROSSED"
            _api("POST", "marginType", {"symbol": symbol, "marginType": mode})
            logger.info(f"✅ 保证金模式: {symbol} {mode}")
        except Exception as e:
            logger.warning(f"保证金设置失败: {e}")

    def calculate_position_size(self, symbol: str, price: float, usdt_amount: float = MAX_POSITION_USDT) -> str:
        """计算合约数量，返回格式化字符串"""
        amount = usdt_amount / price
        return self._adjust_amount(symbol, amount, price)

    def open_long(self, symbol: str, usdt_amount: float = MAX_POSITION_USDT, reason: str = ""):
        return self._open_position(symbol, "BUY", usdt_amount, reason)

    def open_short(self, symbol: str, usdt_amount: float = MAX_POSITION_USDT, reason: str = ""):
        return self._open_position(symbol, "SELL", usdt_amount, reason)

    def _open_position(self, symbol: str, side: str, usdt_amount: float, reason: str = ""):
        """开仓"""
        try:
            price = _get_price(symbol)
            if not price or price <= 0:
                logger.error("❌ 无法获取最新价")
                return None

            amount_str = self.calculate_position_size(symbol, price, usdt_amount)
            amount_f = float(amount_str)
            if amount_f <= 0:
                logger.error("❌ 开仓数量为 0")
                return None

            side_cn = "LONG" if side == "BUY" else "SHORT"

            if self.paper:
                logger.info(f"📋 [模拟] 开仓: {symbol} {side_cn} {amount_str}张 @ {price:.4f} | {reason}")
                self._log_trade({
                    "action": "PAPER_OPEN",
                    "symbol": symbol, "side": side_cn,
                    "amount": amount_str, "price": price,
                    "usdt_value": round(amount_f * price, 2),
                    "reason": reason,
                    "time": datetime.now().isoformat(),
                })
                self._update_paper_position(symbol, side_cn, amount_f, price)
                self._set_stop_loss_take_profit(symbol, side_cn, price, amount_f)
                return {"paper": True, "symbol": symbol, "side": side_cn,
                        "amount": amount_str, "price": price,
                        "usdt_value": round(amount_f * price, 2)}

            # ─── 实盘 ───
            self.set_leverage(symbol)
            self.set_margin_mode(symbol)

            side_map = {"BUY": "LONG", "SELL": "SHORT"}
            order = _api("POST", "order", {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": amount_str,
                "positionSide": side_map.get(side, side),
            })
            logger.info(f"✅ 开仓成功: {order.get('orderId', 'N/A')}")
            self._log_trade({
                "action": "OPEN", "symbol": symbol, "side": side_cn,
                "amount": amount_str, "price": price, "reason": reason,
                "order_id": order.get("orderId"),
                "time": datetime.now().isoformat(),
            })
            time.sleep(1)
            self._set_stop_loss_take_profit(symbol, side_cn, price, amount_f)
            return order

        except Exception as e:
            logger.error(f"❌ 开仓失败: {e}")
            return None

    def _set_stop_loss_take_profit(self, symbol: str, side: str, entry_price: float, amount: float, skip_if_exists: bool = False):
        """
        用 FAPI Algo Order (POST /fapi/v1/algoOrder) 挂止盈止损。
        """
        if skip_if_exists:
            logger.info(f"⏭️ 跳过止盈止损设置 (AI已预挂): {symbol}")
            return
            
        if side == "LONG":
            sl_price = entry_price * (1 - STOP_LOSS_PERCENT / 100)
            tp_price = entry_price * (1 + TAKE_PROFIT_PERCENT / 100)
            sl_side, tp_side = "SELL", "SELL"
        else:
            sl_price = entry_price * (1 + STOP_LOSS_PERCENT / 100)
            tp_price = entry_price * (1 - TAKE_PROFIT_PERCENT / 100)
            sl_side, tp_side = "BUY", "BUY"

        # 对齐到 tickSize
        from trading_bot.exchange.client import _get_symbol_precision
        _, _, pdec, ptick = _get_symbol_precision(symbol)
        sl_price = round(int(sl_price / ptick + 0.5) * ptick, pdec)
        tp_price = round(int(tp_price / ptick + 0.5) * ptick, pdec)
        
        # 挂单需要用 FAPI Algo API
        FAPI_BASE = get_exchange_config().fapi_v1_base
        proxy = {"http": PROXY, "https": PROXY}
        headers = {"X-MBX-APIKEY": API_KEY}
        
        def _post(path, params):
            p = dict(params)
            p["timestamp"] = _ts()
            p["recvWindow"] = 10000
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f"{FAPI_BASE}/{path}?{q}&signature={sig}"
            resp = req.post(url, headers=headers, timeout=15, proxies=proxy)
            if resp.status_code != 200:
                raise Exception(f"Algo {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        
        if self.paper:
            logger.info(f"📋 [模拟] 止损@{sl_price} / 止盈@{tp_price}")
            self._log_trade({"action": "PAPER_SET_SLTP", "symbol": symbol,
                "stop_loss": sl_price, "take_profit": tp_price,
                "time": datetime.now().isoformat()})
            self._update_paper_sltp(symbol, sl_price, tp_price)
            return
        
        try:
            # 止损
            _post("algoOrder", {
                "symbol": symbol, "side": sl_side,
                "positionSide": side,
                "algotype": "CONDITIONAL",
                "type": "STOP_MARKET",
                "quantity": amount,
                "triggerprice": sl_price,
                "workingType": "MARK_PRICE",
            })
            logger.info(f"🛑 止损: {sl_side} @ {sl_price}")
            time.sleep(0.5)
            
            # 止盈
            _post("algoOrder", {
                "symbol": symbol, "side": tp_side,
                "positionSide": side,
                "algotype": "CONDITIONAL",
                "type": "TAKE_PROFIT_MARKET",
                "quantity": amount,
                "triggerprice": tp_price,
                "workingType": "MARK_PRICE",
            })
            logger.info(f"🎯 止盈: {tp_side} @ {tp_price}")
        except Exception as e:
            logger.warning(f"⚠️ 止盈止损失败: {e}")

    def close_position(self, symbol: str):
        """平仓"""
        if self.paper:
            price = _get_price(symbol)
            paper_pos = self._get_paper_position(symbol)
            if not paper_pos:
                logger.info(f"ℹ️ [模拟] {symbol} 无持仓")
                return None

            pnl = 0
            entry = paper_pos["entry_price"]
            amt = paper_pos["amount"]
            if paper_pos["side"] == "LONG":
                pnl = (price - entry) * amt
            else:
                pnl = (entry - price) * amt

            logger.info(f"📋 [模拟] 平仓: {symbol} {paper_pos['side']} {amt}张 "
                       f"入场:{entry:.4f} 出场:{price:.4f} PnL:{pnl:+.2f} USDT")
            self._log_trade({
                "action": "PAPER_CLOSE", "symbol": symbol,
                "side": paper_pos["side"], "amount": amt,
                "entry_price": entry, "exit_price": price,
                "pnl": round(pnl, 2),
                "time": datetime.now().isoformat(),
            })
            self._clear_paper_position(symbol)
            return {"paper": True, "pnl": round(pnl, 2), "symbol": symbol}

        try:
            # 实盘: 获取持仓方向后市价平仓
            pos_info = _api("GET", "positionRisk", {"symbol": symbol})
            for p in pos_info if isinstance(pos_info, list) else [pos_info]:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) <= 0:
                    continue
                side = "SELL" if amt > 0 else "BUY"
                order = _api("POST", "order", {
                    "symbol": symbol, "side": side,
                    "type": "MARKET", "quantity": abs(amt),
                })
                logger.info(f"✅ 平仓成功: {symbol}")
                return order
            logger.info(f"ℹ️ {symbol} 无持仓")
            return None
        except Exception as e:
            logger.error(f"❌ 平仓失败: {e}")
            return None

    # ─────── 模拟仓位管理 ───────

    def _get_paper_position(self, symbol: str) -> Optional[dict]:
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE) as f:
                    state = json.load(f)
        except:
            pass
        return None

    def _log_trade(self, data: dict):
        from trading_bot.core.settings import LOG_FILE
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        self._update_state(data)

    def _update_state(self, data: dict):
        try:
            state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
            if "trades" not in state:
                state["trades"] = []
            state["trades"].append(data)
            state["last_trade"] = data
            state["trades"] = state["trades"][-50:]
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        except:
            pass

    def cancel_all_orders(self, symbol: Optional[str] = None):
        """取消所有挂单"""
        if self.paper:
            logger.info("📋 [模拟] 取消所有挂单")
            return
        try:
            if symbol:
                _api("DELETE", "allOpenOrders", {"symbol": symbol})
                logger.info(f"✅ 已取消 {symbol} 挂单")
            else:
                for s in SYMBOLS:
                    try:
                        _api("DELETE", "allOpenOrders", {"symbol": s})
                    except:
                        pass
                logger.info("✅ 已取消所有挂单")
        except Exception as e:
            logger.error(f"❌ 取消挂单失败: {e}")

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        if self.paper:
            return []
        try:
            sym = symbol or ",".join(SYMBOLS)
            return _api("GET", "openOrders", {"symbol": sym})
        except:
            return []


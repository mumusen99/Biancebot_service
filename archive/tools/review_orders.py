"""
复查挂单 + 条件委托（止盈止损）: 技术分析 + KOL 情绪辅助判断
每次心跳步骤2运行，评估每个挂单以及条件委托是否合理
"""
import json
import time
import logging
import hmac
import hashlib
import urllib.parse
from pathlib import Path

import requests as req
import pandas as pd

from config import API_KEY, API_SECRET, PROXY, DEFAULT_LEVERAGE, STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT
from data_fetcher import fetch_klines, fetch_ticker
from indicators import generate_technical_signals
from trader import _api

# scalp 策略参数（5x, 止盈10%保证金/止损5%保证金）
SCALP_LEVERAGE = 5
SCALP_SL_PCT = 5.0 / SCALP_LEVERAGE    # 1%
SCALP_TP_PCT = 10.0 / SCALP_LEVERAGE   # 2%

logging.basicConfig(level=logging.INFO, format="%(asctime)s [review] %(message)s")
logger = logging.getLogger("review_orders")

BASE_DIR = Path(__file__).parent
SENTIMENT_FILE = BASE_DIR / "sentiment.json"
BOT_STATE_FILE = BASE_DIR / "bot_state.json"

# 挂单时效阈值
MAX_PENDING_SECONDS = 24 * 3600  # 24h
FAPI_BASE = "https://fapi.binance.com/fapi/v1"
_session = req.Session()
_session.proxies = {"http": PROXY, "https": PROXY}

# 策略预设止盈止损百分比（基于价格，已除以杠杆）
SL_PRICE_PCT = STOP_LOSS_PERCENT / DEFAULT_LEVERAGE   # 4%（5x杠杆）
TP_PRICE_PCT = TAKE_PROFIT_PERCENT / DEFAULT_LEVERAGE  # 8%（5x杠杆）


def load_sentiment() -> dict:
    try:
        return json.loads(SENTIMENT_FILE.read_text())
    except:
        return {}


def load_bot_state() -> dict:
    try:
        return json.loads(BOT_STATE_FILE.read_text())
    except:
        return {}


def _get_algo_orders(symbol: str) -> list:
    """查询该币种的所有条件委托（止盈止损）"""
    try:
        p = {"symbol": symbol, "timestamp": int(time.time() * 1000), "recvWindow": 10000}
        q = urllib.parse.urlencode(sorted(p.items()))
        sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = f"{FAPI_BASE}/allAlgoOrders?{q}&signature={sig}"
        resp = _session.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"  ⚠️ 条件委托查询失败({symbol}): {e}")
    return []


def get_coin_tech_score(symbol: str) -> dict:
    """获取单币技术分析得分"""
    try:
        df = fetch_klines(None, symbol, timeframe="1h", limit=100)
        if df is not None and not df.empty and len(df) > 50:
            return generate_technical_signals(df)
    except Exception as e:
        logger.warning(f"  ⚠️ {symbol} 技术分析失败: {e}")
    return {"long_score": 5, "short_score": 5, "signals": ["数据不足"]}


def get_coin_sentiment(symbol: str, sentiment_data: dict) -> str:
    """从 sentiment.json 提取该币情绪"""
    coin = symbol.replace("USDT", "").upper()
    if "coin_sentiment" in sentiment_data:
        cs = sentiment_data["coin_sentiment"]
        if isinstance(cs, dict):
            for k, v in cs.items():
                if k.replace("USDT", "").upper() == coin:
                    return v.get("sentiment", "neutral") if isinstance(v, dict) else "neutral"
    return "neutral"


def _auto_cancel_order(symbol: str, order_id: int, reason: str):
    """自动取消挂单"""
    try:
        from trader import _api
        _api("POST", "order", {
            "symbol": symbol,
            "orderId": order_id,
            "side": "CANCEL",
        })
        msg = f"✅ 自动取消 {symbol} ID{order_id} ({reason})"
        logger.info(msg)
        print(f"    {msg}")
        return True
    except Exception as e:
        # DELETE 方式重试
        try:
            p = {"symbol": symbol, "orderId": order_id,
                 "timestamp": int(time.time() * 1000), "recvWindow": 10000}
            q = urllib.parse.urlencode(sorted(p.items()))
            sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
            url = f"{FAPI_BASE}/order?{q}&signature={sig}"
            resp = _session.delete(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
            if resp.status_code == 200:
                msg = f"✅ 自动取消 {symbol} ID{order_id} ({reason})"
                logger.info(msg)
                print(f"    {msg}")
                return True
        except:
            pass
        logger.warning(f"   ⚠️ 取消失败 {symbol} ID{order_id}: {e}")
        return False


def format_age(order_time_ms: int) -> str:
    elapsed = time.time() - order_time_ms / 1000
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    return f"{h}h{m}m" if h else f"{m}m"


def calc_sltp_prices(entry_price: float, side: str, is_scalp: bool = False):
    """根据策略预设计算止盈止损价"""
    if is_scalp:
        sl_pct = SCALP_SL_PCT
        tp_pct = SCALP_TP_PCT
        rounding = 8
    else:
        sl_pct = SL_PRICE_PCT
        tp_pct = TP_PRICE_PCT
        rounding = 6
    if side == "LONG":
        sl = entry_price * (1 - sl_pct / 100)
        tp = entry_price * (1 + tp_pct / 100)
    else:
        sl = entry_price * (1 + sl_pct / 100)
        tp = entry_price * (1 - tp_pct / 100)
    return round(sl, rounding), round(tp, rounding)


def _auto_place_sltp(symbol: str, side: str, entry_price: float,
                      amount: float, is_scalp: bool):
    """自动补挂止盈止损"""
    sl_price, tp_price = calc_sltp_prices(entry_price, side, is_scalp)
    sl_side = "SELL" if side == "LONG" else "BUY"

    # 对齐数量精度
    from trader import _get_symbol_precision, _load_precisions, _align_sltp, _align_qty
    _load_precisions()
    _, step, _, _ = _get_symbol_precision(symbol)
    step_str = str(step)
    qty_decimals = len(step_str.split(".")[-1]) if "." in step_str else 0
    aligned_qty = round(int(amount / step) * step, qty_decimals)
    qty_str = ("%g" % aligned_qty).replace(",", "")

    # 价格对齐到 tick size + 方向安全化
    sl_price, tp_price = _align_sltp(symbol, sl_price, tp_price, side)

    params_base = {
        "symbol": symbol,
        "positionSide": side,
        "algotype": "CONDITIONAL",
        "quantity": qty_str,
        "workingType": "MARK_PRICE",
    }
    ok = True

    for ord_type, tside, price, label in [
        ("STOP_MARKET", sl_side, sl_price, "止损"),
        ("TAKE_PROFIT_MARKET", sl_side, tp_price, "止盈"),
    ]:
        params = dict(params_base)
        params.update({
            "side": tside,
            "type": ord_type,
            "triggerprice": str(price),
            "timestamp": int(time.time() * 1000),
            "recvWindow": 10000,
        })
        q = urllib.parse.urlencode(sorted(params.items()))
        sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = f"{FAPI_BASE}/algoOrder?{q}&signature={sig}"
        resp = _session.post(url, headers={"X-MBX-APIKEY": API_KEY},
                             timeout=15)
        if resp.status_code == 200:
            logger.info(f"    ✅ 自动补挂{label} {symbol} @ {price}")
            print(f"    ✅ 自动补挂{label} {symbol} @ {price}")
        else:
            logger.warning(f"    ⚠️ 补挂{label}失败: {resp.text[:100]}")
            ok = False
        time.sleep(0.3)

    return ok


def main():
    sentiment_data = load_sentiment()
    bot_state = load_bot_state()
    active_bot_symbols = set(bot_state.get("positions", {}).keys())
    exchange_symbols = set(bot_state.get("live_exchange_symbols", []))

    # ======== 第一部分：普通挂单检查 ========
    try:
        orders = _api("GET", "openOrders")
    except Exception as e:
        print(f"❌ 无法获取挂单: {e}")
        orders = []

    print(f"📋 普通挂单 ({len(orders)}个):" if orders else "📋 无普通挂单")
    print()

    for o in orders:
        sym = o["symbol"]
        side = f"{o['side']}-{o['positionSide']}"
        qty = o["origQty"]
        price = float(o["price"])
        age_str = format_age(o["time"])
        age_seconds = time.time() - o["time"] / 1000

        try:
            ticker = fetch_ticker(None, sym)
            cur_price = ticker["last"] if ticker else None
        except:
            cur_price = None
        diff_pct = (price - cur_price) / cur_price * 100 if cur_price else 0

        tech = get_coin_tech_score(sym)
        long_s, short_s = tech.get("long_score", 5), tech.get("short_score", 5)
        signals = tech.get("signals", [])
        sentiment = get_coin_sentiment(sym, sentiment_data)

        keep_reason, cancel_reason = [], []
        risk_flag = False
        in_bot = sym in active_bot_symbols
        on_exchange = sym in exchange_symbols

        # 时效性
        if age_seconds > MAX_PENDING_SECONDS:
            cancel_reason.append("超24h"); risk_flag = True

        # 技术面
        if "LONG" in side:
            if long_s >= short_s:
                keep_reason.append(f"技术多{long_s}/空{short_s}")
            else:
                cancel_reason.append(f"技术偏空(空{short_s} > 多{long_s})"); risk_flag = True
        elif "SHORT" in side:
            if short_s >= long_s:
                keep_reason.append(f"技术空{short_s}/多{long_s}")
            else:
                cancel_reason.append(f"技术偏多(多{long_s} > 空{short_s})"); risk_flag = True

        # KOL
        if sentiment == "bearish":
            keep_reason.append("KOL bearish→谨慎"); risk_flag = True
        elif sentiment == "bullish":
            keep_reason.append("KOL bullish→期待")

        # 偏离
        if cur_price and abs(diff_pct) > 5:
            keep_reason.append(f"偏离{diff_pct:+.1f}%")

        tag = "📕" if risk_flag else "📗"
        print(f"{tag} {sym} {side} {qty}@{price}")
        print(f"   市价: {cur_price or '?'} | 偏离: {diff_pct:+.2f}% | 挂单: {age_str}")
        print(f"   技术: 多{long_s}分/空{short_s}分 | KOL: {sentiment}")
        if signals:
            print(f"   信号: {' | '.join(signals[:2])}")
        if keep_reason:
            print(f"   ✅ {'; '.join(keep_reason)}")
        if cancel_reason:
            print(f"   ❌ {'; '.join(cancel_reason)}")
        # ─── 自动取消判断 ───
        should_cancel = False
        cancel_why = ""

        # 孤立订单（不在任何策略记录中）
        if not in_bot and not on_exchange:
            print(f"   ⚠️ 未在 bot 持仓 — 孤立订单")
            should_cancel = True
            cancel_why = "孤立订单"

        # 超24h未成交
        if age_seconds > MAX_PENDING_SECONDS:
            should_cancel = True
            cancel_why = "挂单超24h"

        # 技术面强烈反向
        if cancel_reason and not keep_reason:
            should_cancel = True
            cancel_why = "; ".join(cancel_reason)

        # 执行自动取消
        if should_cancel:
            _auto_cancel_order(sym, o["orderId"], cancel_why)

        print()

    # ======== 第二部分：条件委托（止盈止损）检查 ========
    # 使用 v2 获取持仓
    try:
        p = {"timestamp": int(time.time() * 1000), "recvWindow": 10000}
        q = urllib.parse.urlencode(sorted(p.items()))
        sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = f"{FAPI_BASE.rsplit('/v1')[0]}/v2/positionRisk?{q}&signature={sig}"
        resp = _session.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
        positions = resp.json() if resp.status_code == 200 else []
    except Exception as e:
        logger.warning(f"⚠️ 获取持仓失败: {e}")
        positions = []
    active_pos = [p for p in positions if abs(float(p["positionAmt"])) > 0.0001]

    # 无挂单无持仓 → 跳过复查
    if not orders and not active_pos:
        print("📋 无挂单无持仓，跳过复查")
        return

    if active_pos:
        print(f"📋 条件委托检查 ({len(active_pos)}个持仓):")
        print()

        for p in active_pos:
            sym = p["symbol"]
            side = "LONG" if float(p["positionAmt"]) > 0 else "SHORT"
            entry = float(p["entryPrice"])
            cur = float(p["markPrice"])
            amt = abs(float(p["positionAmt"]))
            pnl_pct = float(p.get("unRealizedProfit", 0))
            pnl_pct_str = f"{pnl_pct:.2f}" if pnl_pct else "?"

            # 获取该币的条件委托
            algos = _get_algo_orders(sym)
            active_algos = [a for a in algos if a.get("algoStatus") in ("NEW", "WORKING")]
            # 分离止损和止盈（orderType 字段，如 STOP_MARKET / TAKE_PROFIT_MARKET）
            stop_orders = [a for a in active_algos if "STOP" in a.get("orderType", "").upper()]
            profit_orders = [a for a in active_algos if "PROFIT" in a.get("orderType", "").upper()]

            # 策略预设值
            expected_sl, expected_tp = calc_sltp_prices(entry, side)
            # 当前仓位离止损的距离
            if side == "LONG":
                dist_to_sl = (cur - expected_sl) / cur * 100
                dist_to_tp = (expected_tp - cur) / cur * 100
            else:
                dist_to_sl = (expected_sl - cur) / cur * 100
                dist_to_tp = (cur - expected_tp) / cur * 100

            # 技术分析检查
            tech = get_coin_tech_score(sym)
            long_s, short_s = tech.get("long_score", 5), tech.get("short_score", 5)
            signals = tech.get("signals", [])
            sentiment = get_coin_sentiment(sym, sentiment_data)

            risk_flag = False
            adjust_reason = []

            # 检查是否有缺失的止盈止损，自动补挂
            is_scalp = active_bot_symbols and sym in active_bot_symbols and \
                       bot_state.get("positions", {}).get(sym, {}).get("strategy") == "scalp"

            if not stop_orders:
                adjust_reason.append(f"❌ 缺少止损单!")
                risk_flag = True
                logger.info(f"  🔧 自动补挂{sym}止损...")
                _auto_place_sltp(sym, side, entry,
                                 float(p["positionAmt"]), is_scalp)
            if not profit_orders:
                adjust_reason.append(f"❌ 缺少止盈单!")
                risk_flag = True
                logger.info(f"  🔧 自动补挂{sym}止盈...")
                _auto_place_sltp(sym, side, entry,
                                 float(p["positionAmt"]), is_scalp)

            # 如果技术面转空（LONG仓位），止损可能太宽
            if side == "LONG" and short_s > long_s + 1:
                adjust_reason.append(f"技术转空(空{short_s} > 多{long_s})，考虑收紧止损")
                risk_flag = True
            elif side == "SHORT" and long_s > short_s + 1:
                adjust_reason.append(f"技术转多(多{long_s} > 空{short_s})，考虑收紧止损")
                risk_flag = True

            # 技术面与止损距离
            if side == "LONG" and dist_to_sl < 1:
                adjust_reason.append(f"市价距止损仅{dist_to_sl:.1f}%，关注!")
                risk_flag = True
            elif side == "SHORT" and dist_to_sl < 1:
                adjust_reason.append(f"市价距止损仅{dist_to_sl:.1f}%，关注!")
                risk_flag = True

            # KOL情绪
            if sentiment == "bearish":
                adjust_reason.append("KOL bearish→注意风控")
                risk_flag = True

            # 输出
            tag = "📕" if risk_flag else "📗"
            print(f"{tag} {sym} {side} 入场:{entry} 市价:{cur} 数量:{amt}")
            print(f"   PnL: {pnl_pct}U | 距止损:{dist_to_sl:.1f}% | 距止盈:{dist_to_tp:.1f}%")
            if stop_orders:
                s = stop_orders[0]
                print(f"   止损: {s.get('triggerPrice', '?')} ({s.get('algoStatus', '?')})")
            if profit_orders:
                s = profit_orders[0]
                print(f"   止盈: {s.get('triggerPrice', '?')} ({s.get('algoStatus', '?')})")
            print(f"   策略预设: 止损@{expected_sl} 止盈@{expected_tp}")
            print(f"   技术: 多{long_s}分/空{short_s}分 | KOL: {sentiment}")
            if signals:
                print(f"   信号: {' | '.join(signals[:2])}")
            if adjust_reason:
                print(f"   {'⚠️' if risk_flag else '📝'} {'; '.join(adjust_reason)}")
            print()
    else:
        print("📋 无持仓，无条件委托")


if __name__ == "__main__":
    main()

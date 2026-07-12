#!/usr/bin/env python3
"""
AI 自动交易引擎 v2
===================
安全原则:
  1. 每单必须「先挂止盈止损 → 再开仓」
  2. 开仓前综合评估: 技术面 + 涨跌榜 + 风险审查
  3. 用户手动单 (DOGEUSDT) 跳过不碰
  4. 20U 预算亏完自动停
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests as req
import hmac, hashlib, urllib.parse

from config import (
    BOT_STATE_FILE, ANALYSIS_FILE, SYMBOLS,
    INITIAL_BUDGET, STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT,
    DEFAULT_LEVERAGE, API_KEY, API_SECRET, PROXY,
)

# ─── 杠杆换算: SL/TP 百分比基于保证金, 除以杠杆得真实价格变动 ──
SL_PRICE_PCT = STOP_LOSS_PERCENT / DEFAULT_LEVERAGE     # 保证金损失% / 杠杆 = 价格%
TP_PRICE_PCT = TAKE_PROFIT_PERCENT / DEFAULT_LEVERAGE    # 同上
from data_fetcher import fetch_positions, fetch_all_tickers, fetch_klines, fetch_ticker
from indicators import generate_technical_signals
from trader import Trader, _load_precisions
from notifications import push as push_notification
from kol_coin_sentiment import get_coin_sentiment, update_sentiment_file

SENTIMENT_FILE = Path(__file__).parent / "sentiment.json"

logger = logging.getLogger("auto_trader")

BOT_FILE = Path(__file__).parent / "bot_state.json"
USER_MANUAL_SYMBOLS: set = set()  # 用户手动开的单，永不碰
BLOCKLIST: set = set()  # 🚫 禁止交易的币种（可追加）


# ─── 连接检查 & 梯子自动修复 ──────────────────────

XRAY_DIR = "/tmp/xray"
SUB_SCRIPT = f"{XRAY_DIR}/update-sub.py"


def _check_binance_api(timeout: int = 8) -> bool:
    """测试 Binance API 是否可达"""
    try:
        test_sess = req.Session()
        test_sess.proxies = {"http": PROXY, "https": PROXY}
        r = test_sess.get("https://fapi.binance.com/fapi/v1/ping", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _fix_proxy_and_retry() -> bool:
    """尝试修复梯子（订阅更新），最多一次"""
    logger.warning("🔌 Binance API 不可达，尝试更新梯子节点...")
    try:
        import subprocess
        subprocess.run(["python3", SUB_SCRIPT], cwd=XRAY_DIR, timeout=45, capture_output=True)
        time.sleep(2)  # 等梯子重启完
        if _check_binance_api(timeout=15):
            logger.info("✅ 梯子修复成功，Binance 已可达")
            return True
        else:
            logger.error("❌ 梯子修复后仍不可达")
            return False
    except Exception as e:
        logger.error(f"❌ 梯子修复脚本执行失败: {e}")
        return False


def ensure_connectivity() -> bool:
    """确保 Binance API 可达，自动修复梯子"""
    if _check_binance_api():
        return True
    # 首次失败 -> 尝试修复
    if _fix_proxy_and_retry():
        return True
    # 彻底失败
    push_notification(
        f"🔌 Binance API 不可达，自动修复失败\n时间: {datetime.now().strftime('%H:%M')}\n请检查梯子状态",
        "error"
    )
    return False


# ─── 微信连接保活 ─────────────────────────────────────

def _check_weixin_session() -> bool:
    """检查 iLinkai token 是否有效，返回 True=正常"""
    try:
        acct_path = Path.home() / ".openclaw" / "openclaw-weixin" / "accounts"
        if not acct_path.exists():
            logger.info("⚠️ 微信账号目录不存在，跳过保活")
            return True
        # 只读主账号文件（不含 context-tokens / sync 等辅助文件）
        acct_files = sorted(acct_path.glob("[a-z0-9]*.json"))
        main_file = None
        for f in acct_files:
            name = f.name
            if name.endswith(".json") and "context" not in name and "sync" not in name:
                main_file = f
                break
        if not main_file:
            logger.info("⚠️ 未找到微信主账号文件，跳过保活")
            return True
        with open(main_file) as f:
            acct = json.load(f)
        token = acct.get("token", "")
        base_url = acct.get("baseUrl", "https://ilinkai.weixin.qq.com")
        if not token:
            logger.info("⚠️ 微信token为空，跳过保活")
            return True
        # 走梯子发探测请求
        sess = req.Session()
        sess.proxies = {"http": PROXY, "https": PROXY}
        resp = sess.post(
            f"{base_url}/ilink/bot/getconfig",
            json={
                "ilink_user_id": acct.get("userId", ""),
                "context_token": "",
                "base_info": {"channel_version": "2.4.1", "bot_agent": "OpenClaw"},
            },
            headers={
                "Authorization": f"Bearer {token}",
                "AuthorizationType": "ilink_bot_token",
                "iLink-App-Id": "bot",
                "Content-Type": "application/json",
            },
            timeout=8,
        )
        data = resp.json()
        errcode = data.get("errcode", data.get("ret", 0))
        if errcode == -14:
            logger.error(f"❌ 微信 session 已过期 (errcode=-14)")
            return False
        if errcode == 0:
            logger.info(f"✅ 微信 session 正常")
        else:
            logger.debug(f"微信 session 探测返回 ret={errcode}")
        return True
    except Exception as e:
        logger.debug(f"微信保活检查异常: {e}")
        return True  # 网络问题不阻断，下次再试


def _restart_gateway():
    """重启 OpenClaw gateway 以清除 session pause 状态"""
    logger.warning("🔄 微信 session 过期，正在重启网关...")
    try:
        import subprocess, signal
        # 找到 bun 进程 (飞牛OS 管理的 gateway)
        result = subprocess.run(
            ["pgrep", "-f", "bun /vol2/@appcenter/trim.openclaw/server/index.js"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split()[0])
            os.kill(pid, signal.SIGTERM)
            logger.info(f"📡 已发送 SIGTERM 到 gateway PID={pid}")
            # 等待重启完成
            time.sleep(20)
            logger.info("✅ 网关已重启")
            return True
        else:
            logger.warning("⚠️ 未找到 gateway 进程")
            return False
    except Exception as e:
        logger.error(f"❌ 重启网关失败: {e}")
        return False


def ensure_weixin_alive():
    """微信保活：检查 session → 过期则重启网关"""
    if _check_weixin_session():
        return
    logger.warning("🔄 微信 session 失效，自动恢复中...")
    if _restart_gateway():
        logger.info("✅ 微信保活完成")
    else:
        push_notification(
            f"🔌 微信 session 过期，自动重启失败\n时间: {datetime.now().strftime('%H:%M')}\n请手动重启飞牛OS的OpenClaw",
            "error"
        )


# ─── Algo 订单 API ────────────────────────────────────

def _sign_url(base: str, path: str, params: dict) -> str:
    q = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return f"{base}/{path}?{q}&signature={sig}"

_session = req.Session()
_session.proxies = {"http": PROXY, "https": PROXY}


FAPI_BASE = LIVE_FAPI = "https://fapi.binance.com/fapi/v1"

def _post_algo(path: str, params: dict) -> dict:
    """POST 到 FAPI /fapi/v1/... ，带签名"""
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 10000
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{FAPI_BASE}/{path}?{q}&signature={sig}"
    resp = _session.post(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=15)
    if resp.status_code != 200:
        raise Exception(f"Algo API {resp.status_code}: {resp.text[:200]}")
    return resp.json()

def _delete_algo(params: dict) -> bool:
    """DELETE /fapi/v1/algoOrder"""
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 10000
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{FAPI_BASE}/algoOrder?{q}&signature={sig}"
    resp = _session.delete(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
    return resp.status_code == 200

def _get_algo_orders(symbol: str) -> list:
    """GET /fapi/v1/allAlgoOrders"""
    p = {"symbol": symbol, "timestamp": int(time.time() * 1000), "recvWindow": 10000}
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    url = f"{FAPI_BASE}/allAlgoOrders?{q}&signature={sig}"
    resp = _session.get(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        return data if isinstance(data, list) else []
    return []


# ─── 状态管理 ─────────────────────────────────────────

def load_bot_state() -> dict:
    if BOT_FILE.exists():
        try:
            return json.loads(BOT_FILE.read_text())
        except:
            pass
    return {
        "budget": INITIAL_BUDGET, "initial_budget": INITIAL_BUDGET,
        "positions": {}, "trades": [], "total_pnl": 0.0,
        "stopped": False, "stop_reason": "", "last_check": "",
    }

def save_bot_state(state: dict):
    BOT_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ─── 持仓同步 ─────────────────────────────────────────

def _sync_pending_positions(state: dict, live_positions: list):
    """检测 pending 限价单是否已成交，同步 bot_state"""
    live_syms = {p["symbol"] for p in live_positions}
    changed = False
    for sym, info in state.get("positions", {}).items():
        if info.get("status") == "pending" and sym in live_syms:
            match = [p for p in live_positions if p["symbol"] == sym]
            if match:
                p = match[0]
                filled_entry = float(p.get("entry_price", 0) or 0)
                filled_qty = abs(float(p.get("size", 0) or 0))
                logger.info(f"🔄 限价单已成交: {sym} entry={filled_entry} qty={filled_qty}")
                state["positions"][sym].update({
                    "status": "active",
                    "entry_price": filled_entry,
                    "filled_qty": filled_qty,
                })
                state.setdefault("trades", []).append({
                    "action": "FILLED",
                    "symbol": sym,
                    "side": info.get("side", ""),
                    "entry_price": filled_entry,
                    "filled_qty": filled_qty,
                    "time": datetime.now().isoformat(),
                })
                changed = True
    if changed:
        logger.info("  ✅ 限价单成交同步完成")


def sync_positions(state: dict) -> dict:
    """同步实时持仓，跳过用户手动单"""
    live = fetch_positions()
    bot_syms = set(state.get("positions", {}).keys())
    
    # 记录交易所上所有有持仓的币种（包括用户手动开的），避免重复开单
    state["live_exchange_symbols"] = list({p["symbol"] for p in live})
    
    # 同步 pending→active（限价单成交检测）
    _sync_pending_positions(state, live)
    
    # 初始化累计闭仓盈亏
    if "closed_pnl" not in state:
        state["closed_pnl"] = 0.0
    
    current_pnl = 0.0
    for p in live:
        sym = p["symbol"]
        if sym in USER_MANUAL_SYMBOLS or sym in BLOCKLIST:
            continue
        if sym in bot_syms:
            state["positions"][sym].update({
                "current_price": p["mark_price"],
                "pnl": p["pnl"],
                "pnl_percent": p["pnl_percent"],
                "size": p["size"],
            })
            current_pnl += p["pnl"]
    
    # 已平仓清理 + 记录PnL + 检查重复开仓标记
    live_bot_syms = {p["symbol"] for p in live if p["symbol"] not in USER_MANUAL_SYMBOLS and p["symbol"] not in BLOCKLIST}
    closed = [s for s in state["positions"] if s not in live_bot_syms]
    for sym in closed:
        pos = state["positions"][sym]
        
        # 跳过 pending 限价单（限价单挂单中，还没成交，不删）
        if pos.get("status") == "pending":
            continue
        
        # 记录关仓PnL
        close_pnl = pos.get("pnl", 0) or 0
        state["closed_pnl"] = round(state["closed_pnl"] + close_pnl, 2)
        strat = pos.get("strategy", "main")
        logger.info(f"📌 [{sym}] 已平仓 PnL:{close_pnl:+.2f}U (累计已实现:{state['closed_pnl']:+.2f}U)")
        
        # 记录CLOSE交易
        state.setdefault("trades", []).append({
            "action": "CLOSE",
            "symbol": sym,
            "side": pos.get("side", "?"),
            "strategy": strat,
            "pnl": round(close_pnl, 2),
            "reason": "止损/止盈触发" if abs(close_pnl) > 0.1 else "平仓",
            "entry_price": pos.get("entry_price", 0),
            "time": datetime.now().isoformat(),
        })
        
        # 黑名单：连续2笔亏损 或 单笔亏损>2U
        if close_pnl < -0.01:  # 亏损单
            recent = [t for t in state.get("trades", []) 
                      if t.get("symbol") == sym and t.get("action") == "CLOSE" and t.get("pnl", 0) < 0][-3:]
            if len(recent) >= 2 or close_pnl < -2.0:
                logger.warning(f"  🚫 {sym} 触发黑名单条件 (连续亏损{'/'.join(str(round(t.get('pnl',0),2)) for t in recent)})")
                state.setdefault("blacklist", []).append({
                    "symbol": sym, "reason": f"连续{len(recent)}笔亏损共{sum(t.get('pnl',0) for t in recent):.2f}U",
                    "time": datetime.now().isoformat(),
                })
        
        del state["positions"][sym]
    
    # total_pnl = 当前未实现 + 已实现累计
    total = round(current_pnl + state["closed_pnl"], 2)
    state["total_pnl"] = total
    return state


def check_stop(state: dict) -> bool:
    if state.get("stopped"):
        return True
    total_loss = state["total_pnl"]
    if total_loss <= -INITIAL_BUDGET:
        state["stopped"] = True
        state["stop_reason"] = f"预算耗尽 (总PnL: {total_loss:.2f})"
        logger.warning(f"🛑 机器人已停止: {state['stop_reason']}")
        push_notification(f"🛑 预算耗尽停机!\n总亏损: {abs(total_loss):.2f}U\n预算上限: {INITIAL_BUDGET:.0f}U\n最终盈亏: {state.get('total_pnl',0):+.2f}U\n请手动检查并处理", "emergency")
        save_bot_state(state)
        return True
    used = sum(p.get("amount", 0) for p in state["positions"].values())
    if used >= INITIAL_BUDGET:
        logger.info(f"ℹ️ 仓位已满 ({used:.1f}/{INITIAL_BUDGET}U)")
    return False


# ─── 开单 (先止盈止损 → 再入场) ─────────────────────

def place_sltp_first(symbol: str, side: str, entry_price: float, amount_qty: float) -> bool:
    """
    用 FAPI Algo Order (POST /fapi/v1/algoOrder) 挂止盈止损。
    先挂SL/TP再开仓，返回 True 表示挂单成功。
    """
    side_binance = "BUY"
    position_side = "LONG"
    sl_side = tp_side = "SELL"
    if side == "SELL":
        side_binance = "SELL"
        position_side = "SHORT"
        sl_side = tp_side = "BUY"
    
    sl_price = entry_price * (1 - SL_PRICE_PCT / 100) if side == "BUY" else entry_price * (1 + SL_PRICE_PCT / 100)
    tp_price = entry_price * (1 + TP_PRICE_PCT / 100) if side == "BUY" else entry_price * (1 - TP_PRICE_PCT / 100)
    
    # 对齐到 tick size + 方向安全化
    from trader import _align_sltp, _get_symbol_precision
    sl_price, tp_price = _align_sltp(symbol, sl_price, tp_price, position_side)
    
    # 数量也对齐到 step
    _, step, _, _ = _get_symbol_precision(symbol)
    step_int = int(round(step * 10**8))
    qty_int = int(round(amount_qty * 10**8))
    aligned_qty = int(qty_int / step_int) * step_int / 10**8
    
    logger.info(f"🛡️ 先挂止损@{sl_price} / 止盈@{tp_price}")
    
    try:
        # 止损
        _post_algo("algoOrder", {
            "symbol": symbol, "side": sl_side,
            "positionSide": position_side,
            "algotype": "CONDITIONAL",
            "type": "STOP_MARKET",
            "quantity": aligned_qty,
            "triggerprice": sl_price,
            "workingType": "MARK_PRICE",
        })
        logger.info(f"  ✅ 止损单已挂: {sl_side} @ {sl_price}")
        
        # 止盈
        _post_algo("algoOrder", {
            "symbol": symbol, "side": tp_side,
            "positionSide": position_side,
            "algotype": "CONDITIONAL",
            "type": "TAKE_PROFIT_MARKET",
            "quantity": aligned_qty,
            "triggerprice": tp_price,
            "workingType": "MARK_PRICE",
        })
        logger.info(f"  ✅ 止盈单已挂: {tp_side} @ {tp_price}")
        return True
    except Exception as e:
        logger.error(f"❌ 止盈止损挂单失败: {e}")
        return False


def _has_pending_limit(symbol: str, side: str) -> bool:
    """检查该币种是否已有同方向挂单，防止重复开单"""
    from trader import _api
    try:
        orders = _api("GET", "openOrders", {"symbol": symbol})
        for o in orders:
            if o["side"] == ("BUY" if side == "LONG" else "SELL") and o["positionSide"] == side:
                return True
    except Exception as e:
        logger.warning(f"⚠️ 查挂单失败({symbol}): {e}")
    return False


def get_recent_closes(hours: float = 2) -> list:
    """从交易所 API 获取最近 N 小时内的已平仓记录（含止盈止损标记）"""
    import requests as rq, hmac, hashlib, urllib.parse, time
    prox = {"http": PROXY, "https": PROXY}
    ts = int(time.time() * 1000)
    start = ts - int(hours * 3600 * 1000)
    p = {"timestamp": ts, "recvWindow": 10000, "limit": 500, "startTime": start}
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    try:
        r = rq.get(f"https://fapi.binance.com/fapi/v1/userTrades?{q}&signature={sig}",
                    headers={"X-MBX-APIKEY": API_KEY}, proxies=prox, timeout=10)
        trades = r.json() if r.status_code == 200 else []
    except:
        return []

    closes = []
    for t in trades:
        pnl = float(t["realizedPnl"])
        if abs(pnl) > 0.0001:
            closes.append({
                "sym": t["symbol"],
                "side": t["positionSide"],
                "price": float(t["price"]),
                "qty": float(t["qty"]),
                "pnl": pnl,
                "time": t["time"],
            })
    return closes


def safe_open_position(symbol: str, side: str, usdt_amount: float, reason: str = "", entry_hint: float = 0) -> dict:
    """
    周内安全开仓: 先挂止盈止损 → 再挂LIMIT单等成交
    entry_hint: 技术分析+KOL综合计算的目标入场价
    """
    # 黑名单检查
    if symbol in BLOCKLIST:
        logger.warning(f"🚫 {symbol} 在黑名单中，拒绝开仓")
        return {"success": False, "error": f"{symbol} 在黑名单中"}

    # 防重复：检查是否已有同方向挂单
    if _has_pending_limit(symbol, side):
        logger.info(f"⏭️ {symbol} 已有{side}方向挂单，跳过")
        return {"success": True, "entry_price": 0, "amount": 0, "skipped": True,
                "message": f"已有{side}挂单，跳过"}

    t = fetch_ticker(None, symbol)
    if not t or not t.get("last"):
        return {"success": False, "error": "无法获取价格"}
    cur_price = t["last"]
    
    side_binance = "BUY" if side == "LONG" else "SELL"
    
    _load_precisions()
    from trader import _get_symbol_precision
    _, step, pdec, ptick = _get_symbol_precision(symbol)
    step_int = int(round(step * 10**8))
    
    # 目标入场价：优先用技术分析+KOL综合计算的值
    target_price = entry_hint
    if not target_price or target_price <= 0:
        # 兜底：用当前价附近
        if side == "LONG":
            target_price = round(int((cur_price * 0.995) / ptick + 0.5) * ptick, pdec)
        else:
            target_price = round(int((cur_price * 1.005) / ptick + 0.5) * ptick, pdec)
    else:
        target_price = round(int(target_price / ptick + 0.5) * ptick, pdec)
    
    # 计算数量
    qty_int = int(round((usdt_amount / target_price) * 10**8))
    steps = int(qty_int / step_int)
    if steps < 1:
        steps = 1
    aligned_qty = steps * step_int / 10**8
    
    # 先挂止盈止损（以目标入场价计算，等成交后自动生效）
    if not place_sltp_first(symbol, side_binance, target_price, aligned_qty):
        return {"success": False, "error": "SL/TP挂单失败"}
    
    # LIMIT 挂单开仓
    logger.info(f"📌 挂LIMIT单: {symbol} {side} {aligned_qty}张 @ {target_price} (市价{cur_price:.2f})")
    
    from trader import _api
    side_map = {"LONG": "BUY", "SHORT": "SELL"}
    
    order = _api("POST", "order", {
        "symbol": symbol,
        "side": side_map[side],
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": aligned_qty,
        "price": target_price,
        "positionSide": side,
    })
    
    logger.info(f"✅ LIMIT单已挂: {order.get('orderId', 'N/A')} @ {target_price}")
    
    # 存状态
    from trader import Trader
    trader = Trader()
    trader._log_trade({
        "action": "LIMIT_OPEN", "symbol": symbol, "side": side,
        "amount": str(aligned_qty), "price": target_price,
        "reason": reason,
        "order_id": order.get("orderId"),
        "time": datetime.now().isoformat(),
    })
    
    push_notification(f"📌 挂单等待成交: {symbol} {side}\n价格: {target_price}\n数量: {aligned_qty}\n理由: {reason[:60]}", "limit")
    
    return {"success": True, "entry_price": target_price, "amount": usdt_amount,
            "limit_order": True, "order_id": order.get("orderId"),
            "message": f"LIMIT单已挂 @ {target_price}, 等待成交"}


# ─── 市场扫描与风险评分 ─────────────────────────────

def scan_with_risk() -> list:
    """
    扫描全市场 USDT 合约
    筛选策略: 成交量TOP60 + 24h涨跌幅排序 → 前20名做技术深析
    返回: 按综合评分排序的信号列表
    """
    # 加载黑名单
    try:
        bs = json.loads(BOT_STATE_FILE.read_text())
        for item in bs.get("blacklist", []):
            if item.get("symbol") and item["symbol"] not in BLOCKLIST:
                logger.info(f"  🚫 黑名单币种: {item['symbol']} ({item.get('reason','')})")
                BLOCKLIST.add(item["symbol"])
    except:
        pass
    
    tickers = fetch_all_tickers()
    
    # 第一步: 全市场快筛 — 按成交量排序
    all_coins = []
    for sym, t in tickers.items():
        if not sym.endswith("USDT") or sym in USER_MANUAL_SYMBOLS or sym in BLOCKLIST:
            continue
        vol = t.get("volume24h", 0) or 0
        chg = t.get("change24h", 0) or 0
        if vol > 0:
            all_coins.append({"symbol": sym, "volume": vol, "change24h": chg, "price": t.get("last", 0)})
    
    # 按成交量排序取前60
    by_vol = sorted(all_coins, key=lambda x: x["volume"], reverse=True)[:60]
    # 按24h涨跌绝对值排序
    by_move = sorted(all_coins, key=lambda x: abs(x["change24h"]), reverse=True)
    
    # 合并取前20个候选: 成交量前30 + 涨跌幅前20 (去重)
    candidates = {}
    for item in by_vol[:30]:
        candidates[item["symbol"]] = item
    for item in by_move[:20]:
        candidates[item["symbol"]] = item
    # 必须包含用户关注的16个币
    for sym in SYMBOLS:
        if sym in tickers:
            candidates[sym] = {"symbol": sym, "volume": tickers[sym].get("volume24h",0), "change24h": tickers[sym].get("change24h",0), "price": tickers[sym].get("last",0)}
    
    logger.info(f"📊 全市场筛选: {len(all_coins)}个合约 → {len(candidates)}个候选")
    
    # 第二步: 对候选做周内技术深析（1h + 4h），10线程并发
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    def analyze_one(symbol, info):
        try:
            df_1h = fetch_klines(None, symbol, "1h", 100)
            df_4h = fetch_klines(None, symbol, "4h", 80)
            df_1d = fetch_klines(None, symbol, "1d", 30)
            if df_1h.empty or df_4h.empty:
                return None
            
            sig1h = generate_technical_signals(df_1h)
            sig4h = generate_technical_signals(df_4h)
            sig1d = generate_technical_signals(df_1d) if not df_1d.empty else None
            
            risk = 0
            abs_chg = abs(info.get("change7d", info["change24h"]))
            if abs_chg > 30: risk += 3
            elif abs_chg > 15: risk += 2
            elif abs_chg > 8: risk += 1
            
            vol_rank = sum(1 for c in all_coins if c["volume"] > info["volume"]) / max(len(all_coins), 1)
            attention_score = round((1 - vol_rank) * 6 + min(abs_chg / 8, 4), 1)
            
            score_long = sig1h["long_score"] + sig4h["long_score"] * 1.5
            score_short = sig1h["short_score"] + sig4h["short_score"] * 1.5
            if sig1d:
                score_long += sig1d["long_score"] * 0.5
                score_short += sig1d["short_score"] * 0.5
            
            return {
                "symbol": symbol,
                "price": info["price"],
                "change24h": info["change24h"],
                "volume24h": info["volume"],
                "attention": attention_score,
                "1h_long": sig1h["long_score"],
                "1h_short": sig1h["short_score"],
                "4h_long": sig4h["long_score"],
                "4h_short": sig4h["short_score"],
                "1h_trend": sig1h["trend"],
                "4h_trend": sig4h["trend"],
                "score_long": round(score_long, 1),
                "score_short": round(score_short, 1),
                "risk": risk,
                "signals_1h": sig1h["signals"][:2],
                "signals_4h": sig4h["signals"][:2],
                "support_1h": sig1h.get("support", 0),
                "resistance_1h": sig1h.get("resistance", 0),
                "support_4h": sig4h.get("support", 0),
                "resistance_4h": sig4h.get("resistance", 0),
                "last_price_1h": sig1h.get("last_price", info["price"]),
                "last_price_4h": sig4h.get("last_price", info["price"]),
            }
        except Exception as e:
            logger.debug(f"扫描 {symbol} 失败: {e}")
            return None
    
    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(analyze_one, sym, info): sym for sym, info in candidates.items()}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)
    
    # 按综合评分排序
    results.sort(key=lambda x: max(x["score_long"], x["score_short"]) + x["attention"], reverse=True)
    logger.info(f"  波动最大: {', '.join(r['symbol'] for r in results[:5])}")
    return results


def load_sentiment() -> dict:
    """读取 KOL 情绪分析结果"""
    try:
        if SENTIMENT_FILE.exists():
            return json.loads(SENTIMENT_FILE.read_text())
    except:
        pass
    return {"overall_sentiment": "neutral", "risk_warning": False, "last_update": None}




def calc_entry_price(cand, side, cur_price, kol_score=0):
    """
    基于技术分析+ KOL 计算最优挂单价（周内级别）。
    返回: (entry_price, reason)
    """
    cur = cur_price
    if side == "LONG":
        refs = []
        s1 = cand.get("support_1h", 0)
        if s1 > 0 and s1 < cur * 0.98:
            refs.append((s1, "1h支撑"))
        s4 = cand.get("support_4h", 0)
        if s4 > 0 and s4 < cur * 0.98 and s4 != s1:
            refs.append((s4, "4h支撑"))
        refs.append((cur * 0.995, "微幅回调"))
        if kol_score > 50:
            discount = 0.003
        elif kol_score > 20:
            discount = 0.005
        else:
            discount = 0.01
        refs.append((cur * (1 - discount), f"KOL调整{discount*100:.1f}%"))
        entry = min(p for p, _ in refs)
        reasons = [r for p, r in refs if abs(p - entry) / max(p, 0.01) < 0.02]
        rs = "+".join(reasons[:2])
        return entry, f"挂单@{entry:.4f} ({rs})" if reasons else f"挂单@{entry:.4f}"
    else:
        refs = []
        r1 = cand.get("resistance_1h", 0)
        if r1 > 0 and r1 > cur * 1.02:
            refs.append((r1, "1h阻力"))
        r4 = cand.get("resistance_4h", 0)
        if r4 > 0 and r4 > cur * 1.02 and r4 != r1:
            refs.append((r4, "4h阻力"))
        refs.append((cur * 1.005, "微幅反弹"))
        if kol_score < -50:
            premium = 0.003
        elif kol_score < -20:
            premium = 0.005
        else:
            premium = 0.01
        refs.append((cur * (1 + premium), f"KOL调整{premium*100:.1f}%"))
        entry = max(p for p, _ in refs)
        reasons = [r for p, r in refs if abs(p - entry) / max(p, 0.01) < 0.02]
        rs = "+".join(reasons[:2])
        return entry, f"挂单@{entry:.4f} ({rs})" if reasons else f"挂单@{entry:.4f}"


def _build(cand, side, score, sigs, amount, repl, kol_info="", kol_score=0):
    """构建开仓决策返回值"""
    parts = [f"技术{score}分"]
    parts.append(" ".join(sigs[:2]))
    if kol_info:
        parts.append(kol_info)
    cur_price = cand.get("last_price_1h", cand.get("price", 0))
    entry_hint, entry_reason = calc_entry_price(cand, side, cur_price, kol_score)
    parts.append(entry_reason)
    if repl and repl.get("ok"):
        parts.append(f"换仓{repl['close_sym']}")
        return {"action": "REPLACE", "symbol": cand["symbol"], "side": side,
                "score": score, "usdt_amount": amount,
                "close_symbol": repl["close_sym"], "reason": " | ".join(parts),
                "entry_hint": entry_hint}
    return {"action": "OPEN", "symbol": cand["symbol"], "side": side,
            "score": score, "usdt_amount": amount, "reason": " | ".join(parts),
            "entry_hint": entry_hint}

def decide(opportunities: list, state: dict) -> dict:
    """
    Phase 1: 技术面分析 → 找出最佳开仓点位
    Phase 2: KOL情绪 → 二次风控判断是否执行
    """
    if state.get("stopped"):
        return {"action": "HOLD", "reason": "机器人已停止"}
    
    used = sum(p.get("amount", 0) for p in state["positions"].values())
    remaining = max(0, INITIAL_BUDGET - used)
    slots_left = max(1, 5 - len(state["positions"]))
    per_trade_max = max(5, remaining / slots_left) if remaining > 0 else 10
    
    sentiment = load_sentiment()
    sent = sentiment.get("overall_sentiment", "neutral")
    risk_warn = sentiment.get("risk_warning", False)
    details = sentiment.get("details", {})
    bear_pct = details.get("bearish_pct", 50)
    bull_pct = details.get("bullish_pct", 50)
    
    # ─── Phase 1: 技术面优先筛选（周内级别加强） ───
    TECH_BASE_THRESHOLD = 8     # 提高到8分
    DIRECTION_GAP = 3            # 方向差距拉到3分
    
    def _combined_score(o):
        return max(o["score_long"], o["score_short"]) + o["attention"]
    
    candidates = sorted([o for o in opportunities if o["risk"] < 2],
                        key=_combined_score, reverse=True)
    
    top_volatile_syms = set(x["symbol"] for x in
        sorted(opportunities, key=lambda x: abs(x["change24h"]), reverse=True)[:5])
    logger.info(f" 波动最大: {', '.join(top_volatile_syms)}")
    
    live_syms = set(state.get("live_exchange_symbols", []))
    candidates = [c for c in candidates if c["symbol"] not in live_syms]
    
    # ─── Phase 2: KOL 情绪二次风控 ──────────────────
    # 对每个技术面合格的候选逐一检查 KOL 态度
    kol_level = "neutral"
    if risk_warn:
        if bear_pct > bull_pct + 30:
            kol_level = "strong_bearish"
        elif bear_pct > bull_pct + 10:
            kol_level = "mild_bearish"
        elif bull_pct > bear_pct + 30:
            kol_level = "strong_bullish"
        elif bull_pct > bear_pct + 10:
            kol_level = "mild_bullish"
    else:
        kol_level = sent  # bullish / bearish / neutral
    
    # ─── Phase 1.5: 针对性币种KOL搜索 ──────────────
    # 只对技术面 Top 6 候选做针对性检索（平衡速度与精度）
    top_candidates = [c["symbol"] for c in candidates[:6]]
    if top_candidates:
        logger.info(f" 🔬 检索 {len(top_candidates)}个币的KOL情绪（网络搜索）...")
        coin_sent = get_coin_sentiment(top_candidates)
        update_sentiment_file(coin_sent)
        # 重新读取（包含币种数据）
        sentiment = load_sentiment()
        coin_sentiment = sentiment.get("coin_sentiment", {})
        for sym, data in sorted(coin_sentiment.items(), key=lambda x: abs(x[1].get("score", 0)), reverse=True):
            icon = "🟢" if data["sentiment"] == "bullish" else "🔴" if data["sentiment"] == "bearish" else "⚪"
            score = data.get("score", 0)
            logger.info(f"   {icon} {sym}: {data['sentiment']:8s} (得分{score:+.0f})")
    else:
        coin_sentiment = {}
    
    logger.info(f" KOL情绪: {sent} | 多{bull_pct}%/空{bear_pct}% | 风险{'⚠️' if risk_warn else '✅'} | 判定={kol_level}")
    
    # 换仓评估
    def _try_replace(cand):
        if not state["positions"] or remaining >= 10:
            return {"ok": False}
        worst = min(state["positions"].items(), key=lambda x: x[1].get("pnl_percent", 0))
        ws, wp = worst
        worst_pnl = wp.get("pnl_percent", 0)
        new_score = max(cand["score_long"], cand["score_short"])
        old_score = wp.get("score_when_opened", 0)
        if worst_pnl < -5 or (new_score - old_score >= 7):
            return {"ok": True, "close_sym": ws, "reason": f"{ws}亏损{worst_pnl:.1f}%" if worst_pnl < -5 else f"{ws}旧{old_score}分"}
        return {"ok": False}
    
    for cand in candidates:
        chg24h = abs(cand.get("change24h", 0))
        
        # 判断最佳方向
        if cand["score_long"] >= cand["score_short"]:
            side = "LONG"
            score = cand["score_long"]
        else:
            side = "SHORT"
            score = cand["score_short"]
        
        # ─── 技术面过滤（Phase 1 核心）───────────────
        # 1. 基础技术分门槛（放宽到7分）
        if score < TECH_BASE_THRESHOLD:
            continue
        
        # 2. 方向差距检查
        gap = cand["score_long"] - cand["score_short"]
        if side == "LONG" and gap < DIRECTION_GAP:
            continue
        if side == "SHORT" and -gap < DIRECTION_GAP:
            continue
        
        # 3. 周内级别：1h不反向，4h必须同向
        t1h = cand.get("1h_trend", cand.get("5m_trend"))
        t4h = cand.get("4h_trend", cand.get("15m_trend"))
        if side == "LONG" and t1h == "SHORT":
            continue
        if side == "SHORT" and t1h == "LONG":
            continue
        # 周内要求4h趋势同向（不再允许NEUTRAL）
        if t4h != side:
            continue
        
        # 4. 24h 涨跌幅过大不碰
        if chg24h > 8:
            continue
        
        # 5. 高波动列表需高分
        if cand["symbol"] in top_volatile_syms and score < 9:
            continue
        
        # ─── Phase 2: 针对性 KOL 风控 ────────────────
        kol_gate_result = ""
        kol_penalty = 0
        kol_factor = 1.0
        
        coin_kol = coin_sentiment.get(cand["symbol"], {})
        cs_score = 0  # kol_score for entry pricing, default=neutral
        if coin_kol and coin_kol.get("sentiment"):
            cs = coin_kol["sentiment"]
            cs_score = coin_kol.get("score", 0)
            cs_risk = coin_kol.get("risk", False)
            kol_gate_result = f"KOL{cs}({cs_score:+.0f})"
            
            if cs_risk:
                logger.debug(f"  针对性KOL风险警告: {cand['symbol']}")
                if side == "LONG" and cs_score < 0:
                    continue
                if side == "SHORT" and cs_score > 0:
                    continue
                kol_factor = 0.5
            
            if side == "LONG":
                if cs_score < -20:
                    # KOL强烈看空该币 → 做多直接跳过（不再罚分通过）
                    logger.debug(f" KOL强看空{cs_score:+.0f}, 跳过做多 {cand['symbol']}")
                    continue
                elif cs_score < 0:
                    kol_penalty = 3
                elif cs_score > 30:
                    kol_penalty = -2  # 强看多, 大减门槛
                elif cs_score > 20:
                    kol_penalty = -1
                # 0~20: 中性偏多, 不罚
            else:  # SHORT
                if cs_score > 20:
                    # KOL强烈看多该币 → 做空直接跳过
                    logger.debug(f" KOL强看多{cs_score:+.0f}, 跳过做空 {cand['symbol']}")
                    continue
                elif cs_score > 0:
                    kol_penalty = 3
                elif cs_score < -30:
                    kol_penalty = -2  # 强看空, 大减门槛
                elif cs_score < -20:
                    kol_penalty = -1
                # -20~0: 中性偏空, 不罚
        else:
            # 无针对性数据，用全局情绪
            if kol_level == "strong_bearish":
                if side == "LONG":
                    continue
                kol_penalty = 2
                kol_factor = 0.5
            elif kol_level == "mild_bearish":
                if side == "LONG":
                    kol_penalty = 3
                    kol_factor = 0.7
                else:
                    kol_penalty = -1
            elif kol_level == "strong_bullish":
                if side == "SHORT":
                    continue
                kol_penalty = 2
                kol_factor = 0.5
            elif kol_level == "mild_bullish":
                if side == "SHORT":
                    kol_penalty = 3
                    kol_factor = 0.7
                else:
                    kol_penalty = -1
            elif kol_level == "bearish":
                if side == "LONG":
                    kol_penalty = 2
                    kol_factor = 0.8
            elif kol_level == "bullish":
                if side == "SHORT":
                    kol_penalty = 2
                    kol_factor = 0.8
            kol_gate_result = f"KOL全局={kol_level}"
        
        effective_score = score - kol_penalty
        if effective_score < TECH_BASE_THRESHOLD:
            logger.debug(f" KOL否决: {cand['symbol']} 技术{score}->{effective_score} 惩罚{kol_penalty} {kol_gate_result}")
            continue
        
        # ─── 预算和仓位计算 ────────────────────────
        # 波段统一20U保证金，3x杠杆
        amount = 20.0
        
        # 周内挂单传入KOL分数调整入场价
        sigs = cand.get("signals_1h", cand.get("signals_5m", []))
        repl = _try_replace(cand)
        return _build(cand, side, score, sigs, amount, repl, kol_gate_result, cs_score)
    
    return {"action": "HOLD", "reason": "技术/KOL综合过滤后无信号"}

def _cancel_algo_orders(symbol: str):
    """取消某个币的所有止盈止损单 (DELETE /fapi/v1/allAlgoOrders)"""
    try:
        p = {"symbol": symbol, "timestamp": int(time.time() * 1000), "recvWindow": 10000}
        q = urllib.parse.urlencode(sorted(p.items()))
        sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = f"{FAPI_BASE}/allAlgoOrders?{q}&signature={sig}"
        resp = _session.delete(url, headers={"X-MBX-APIKEY": API_KEY}, timeout=10)
        return resp.status_code == 200
    except:
        return False


def _adjust_sltp(symbol: str, side: str, entry_price: float, current_price: float, pnl_pct: float, pos: dict, state: dict):
    """
    动态调整止盈止损点
    trailing: 盈利越多，止损越往上移，锁定利润
    使用 FAPI Algo Order (STOP_MARKET) 更新止损单
    """
    side_cn = side
    sl_side = "SELL" if side == "LONG" else "BUY"
    
    # 计算新的止损价 (追踪止盈)
    if side == "LONG":
        base_sl = entry_price * (1 - SL_PRICE_PCT / 100)
        if pnl_pct > 8:
            new_sl = entry_price * (1 + 6 / DEFAULT_LEVERAGE / 100)
        elif pnl_pct > 5:
            new_sl = entry_price * (1 + 3 / DEFAULT_LEVERAGE / 100)
        elif pnl_pct > 3:
            new_sl = entry_price * (1 + 1 / DEFAULT_LEVERAGE / 100)
        elif pnl_pct > 0:
            new_sl = entry_price * (1 - 0.3 / DEFAULT_LEVERAGE / 100)
        else:
            new_sl = base_sl
    else:
        base_sl = entry_price * (1 + SL_PRICE_PCT / 100)
        if pnl_pct > 8:
            new_sl = entry_price * (1 - 6 / DEFAULT_LEVERAGE / 100)
        elif pnl_pct > 5:
            new_sl = entry_price * (1 - 3 / DEFAULT_LEVERAGE / 100)
        elif pnl_pct > 3:
            new_sl = entry_price * (1 - 1 / DEFAULT_LEVERAGE / 100)
        elif pnl_pct > 0:
            new_sl = entry_price * (1 + 0.3 / DEFAULT_LEVERAGE / 100)
        else:
            new_sl = base_sl
    
    last_sl = pos.get("last_sl", 0)
    
    threshold = 0.003 * entry_price
    should = False
    if side == "LONG" and (new_sl - last_sl) > threshold:
        should = True
    elif side == "SHORT" and (last_sl - new_sl) > threshold:
        should = True
    elif last_sl == 0:
        should = True
    
    if not should:
        return
    
    # 对齐到 tick size
    from trader import _align_price_dir
    new_sl = _align_price_dir(symbol, new_sl, 'nearest')
    qty = pos.get("size", 0)
    
    logger.info(f"🔄 追踪止损: {symbol} PnL{pnl_pct:+.1f}% → 止损移至 {new_sl}")
    
    try:
        # 先取消旧的止损单，再挂新的
        old_orders = _get_algo_orders(symbol)
        for o in old_orders:
            if o.get("orderType") == "STOP_MARKET" and o.get("algoStatus") == "NEW":
                _delete_algo({"symbol": symbol, "algoId": o["algoId"]})
                time.sleep(0.3)
        
        # 挂新的止损
        _post_algo("algoOrder", {
            "symbol": symbol, "side": sl_side,
            "positionSide": side_cn,
            "algotype": "CONDITIONAL",
            "type": "STOP_MARKET",
            "quantity": qty,
            "triggerprice": new_sl,
            "workingType": "MARK_PRICE",
        })
        
        if symbol in state["positions"]:
            state["positions"][symbol]["last_sl"] = new_sl
        logger.info(f"  ✅ 止损已移至 {new_sl}")
    except Exception as e:
        logger.warning(f"  ⚠️ 追踪止损失败: {e}")


def manage_positions(state: dict):
    """
    仓位管理: 止盈止损 + 动态止损调整
    每周期检查:
      1. 是否触发了止盈/止损 → 平仓
      2. 是否有利润需要追踪止损 → 移动止损位
    """
    trader = Trader()
    
    for sym, pos in list(state["positions"].items()):
        pnl_pct = pos.get("pnl_percent", 0)
        entry = pos.get("entry_price", 0)
        current = pos.get("current_price", entry)
        side = pos.get("side", "LONG")
        
        # 第一步: 检查是否触发止盈止损
        if pnl_pct >= TAKE_PROFIT_PERCENT:
            logger.info(f"🎯 止盈: {sym} +{pnl_pct:.1f}%")
            profit = pos.get("pnl", 0)
            trader.close_position(sym)
            state["trades"].append({"action":"CLOSE","symbol":sym,"reason":f"止盈+{pnl_pct:.1f}%","time":datetime.now().isoformat()})
            state["total_pnl"] += profit
            del state["positions"][sym]
            push_notification(f"🎯 止盈平仓: {sym}\n方向: {pos.get('side','?')}\n盈亏: +{pnl_pct:.1f}% ({profit:+.2f}U)", "tp")
            continue
        
        if pnl_pct <= -STOP_LOSS_PERCENT:
            logger.info(f"🛑 止损: {sym} {pnl_pct:.1f}%")
            loss = pos.get("pnl", 0)
            trader.close_position(sym)
            state["trades"].append({"action":"CLOSE","symbol":sym,"reason":f"止损{pnl_pct:.1f}%","time":datetime.now().isoformat()})
            state["total_pnl"] += loss
            del state["positions"][sym]
            push_notification(f"🛑 止损平仓: {sym}\n方向: {pos.get('side','?')}\n盈亏: {pnl_pct:.1f}% ({loss:+.2f}U)", "sl")
            continue
        
        # 第二步: 动态调整止损 (追踪止盈)
        if current and entry and current > 0:
            _adjust_sltp(sym, side, entry, current, pnl_pct, pos, state)
    
    return state


def run_cycle():
    # 0. 确保 Binance API 可达（断线自动修复梯子）
    if not ensure_connectivity():
        logger.error("🔌 Binance API 不可达，跳过本轮")
        return
    
    # 0.5 微信 session 保活检查（过期自动重启网关）
    ensure_weixin_alive()
    
    state = load_bot_state()
    state["last_check"] = datetime.now().isoformat()
    
    state = sync_positions(state)
    state = manage_positions(state)
    if check_stop(state):
        save_bot_state(state)
        return
    
    opportunities = scan_with_risk()
    decision = decide(opportunities, state)
    
    if decision["action"] in ("OPEN", "REPLACE"):
        trader = Trader()
        # REPLACE: 先平旧仓再开新仓
        if decision["action"] == "REPLACE":
            cs = decision.get("close_symbol", "")
            if cs and cs in state["positions"]:
                logger.info(f"🔄 换仓: 先平 {cs}")
                trader.close_position(cs)
                state["trades"].append({"action":"CLOSE","symbol":cs,"reason":f"换仓→{decision['symbol']}","time":datetime.now().isoformat()})
                state["positions"].pop(cs, None)
                try:
                    _cancel_algo_orders(cs)
                except:
                    pass
                time.sleep(1)
        
        entry_hint = decision.get("entry_hint", 0)
        result = safe_open_position(decision["symbol"], decision["side"], decision["usdt_amount"], decision.get("reason", ""), entry_hint)
        label = "换仓" if decision["action"] == "REPLACE" else "新开仓"
        
        # 已存在挂单则跳过
        if result.get("skipped"):
            logger.info(f"⏭️ 跳过({decision['symbol']}): {result.get('message', '')}")
        elif result["success"]:
            state["positions"][decision["symbol"]] = {
                "side": decision["side"], "amount": decision["usdt_amount"],
                "entry_price": result["entry_price"], "opened_at": datetime.now().isoformat(),
                "reason": decision["reason"],
                "score_when_opened": decision.get("score", 0),
                "status": "pending",  # 限价单挂单中，未成交
            }
            state["trades"].append({"action":"OPEN","symbol":decision["symbol"],"side":decision["side"],
                                     "amount":decision["usdt_amount"],"reason":decision["reason"],
                                     "time":datetime.now().isoformat()})
            logger.info(f"✅ [AI] {label}: {decision['symbol']} {decision['side']} (LIMIT挂单，等待成交)")
            push_notification(f"📌 挂单等待成交: {decision['symbol']} {decision['side']}\n价格: {result.get('entry_price','?')}\n数量: {decision['usdt_amount']}U\n理由: {decision['reason']}", "limit")
        else:
            error_msg = result.get('error', '未知')
            logger.warning(f"❌ [AI] 开单失败: {error_msg}")
            push_notification(f"❌ 开单失败: {decision['symbol']} {decision['side']}\n金额: {decision['usdt_amount']}U\n理由: {decision['reason']}\n错误: {error_msg}", "error")
    else:
        logger.info(f"⏸️ {decision.get('reason', '')}")
    
    save_bot_state(state)
    used = sum(p.get("amount", 0) for p in state["positions"].values())
    print(f"  [AI] 预算80U | 已用{used:.1f}U | PnL {state['total_pnl']:+.2f}U | 持仓{len(state['positions'])}个")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    run_cycle()

"""
实时量化交易系统 v4
===================
基于 R 值风控的五种交易类型框架。
引用交易算法完整版文档。

架构:
  strategy/  → 路由, SL/TP, 过滤器
  risk/      → 冷却, 模式反馈
  execution/ → 交易计划, 预检查
  rt_scalper.py  → 主循环

数据: 1m/5m/15m K线, REST API 轮询
风控: 硬止损 + 软止损 + 时间止损 + 连亏保护 + 模式反馈 + 冷却
"""

import json, time, logging, os, sys, signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
os.chdir(str(BASE))

log_file = BASE / "rt_scalper.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [v4] %(message)s",
    force=True,
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
)
logger = logging.getLogger("v4")

from config import API_KEY, API_SECRET, PROXY
from trader import _api, _align_sltp, _align_price, _align_qty, _load_precisions
from data_fetcher import fetch_klines, fetch_ticker, fetch_all_tickers
from scalper import compute_indicators
from market_regime import get_btc_environment

from strategy.market_state import (
    update_market_state, get_global_permission, check_profit_lock,
    check_loss_cooldown, market_state_summary, get_state,
)
from strategy.trade_router import (
    Direction, TradeType, MarketRegime,
    classify_direction, route_trade_type, SymbolData,
    ema as calc_ema, slope as calc_slope,
)
from strategy.sl_tp import calc_sl_tp, estimated_cost_pct, net_profit_ok
from strategy.filters import (
    position_filter, btc_dominance_filter, overheat_filter, btc_trend_flip_filter,
    sector_confirm_filter,
)
from risk.cooldown import (
    account_allows_trade, account_risk_multiplier,
    symbol_allows_trade, symbol_risk_multiplier,
    mode_allows_trade, mode_risk_multiplier,
    mode_quota_allows,
    record_symbol_result, record_mode_result,
    update_account_result,
)
from execution.order_planner import build_trade_plan, TradePlan, calc_position_value

# ─── 固定参数 ─────────────────────────────────────
LEVERAGE = 5
MARGIN_PER_TIER = {"probe": 5, "standard": 10, "strong_trend": 15}
MAX_POSITIONS = 10

# ─── 全局状态 ─────────────────────────────────────
running = True
price_cache = {}        # {sym: price}
position_state = {}     # {sym: {side, entry, sl_hard, sl_soft, tp1, tp2, qty, trade_type, ...}}
watch_zone = {}         # {sym: {added_at, score}}
sector_map = {}         # {sym: sector} — 懒加载


def _handle_exit(sig, frame):
    global running
    logger.info("👋 关闭中...")
    running = False
signal.signal(signal.SIGTERM, _handle_exit)
signal.signal(signal.SIGINT, _handle_exit)


# ═══════════════════════════════════════════════════════════
#  1. 市场状态
# ═══════════════════════════════════════════════════════════

def analyze_market(symbol="BTCUSDT") -> dict:
    """
分析市场状态 (4章)"""

    result = {"regime": "range", "heat": "normal", "bias": 0, "trend_strength": 0}
    try:
        env = get_btc_environment()
        result["bias"] = env.get("bias", 0)
        df_15m = fetch_klines(None, symbol, "15m", 40)
        if df_15m is None or df_15m.empty: return result
        df_15m = compute_indicators(df_15m)
        last = df_15m.iloc[-1]
        close = float(last["close"])
        atr = float(last.get("atr", close * 0.005))
        ema9 = float(last.get("ema9", close))
        ema21 = float(last.get("ema21", close))
        slope_21 = (ema21 - float(df_15m.iloc[-8].get("ema21", ema21))) / max(ema21, 0.01) * 100
        trend_strength = abs(slope_21) * 50
        atr_ratio = atr / close * 100

        result["trend_up"] = ema9 > ema21
        result["trend_down"] = ema9 < ema21
        result["trend_strength"] = round(trend_strength, 1)
        result["atr_ratio"] = round(atr_ratio, 2)

        if atr_ratio > 3.0:
            result["regime"] = "high_volatility"
        elif trend_strength >= 15:
            result["regime"] = "trend"
        else:
            result["regime"] = "range"

        bias = result["bias"]
        result["heat"] = "high" if abs(bias) >= 5 else ("normal" if abs(bias) >= 2 else "low")
    except Exception as e:
        logger.debug(f"分析市场失败: {e}")
    return result


# ═══════════════════════════════════════════════════════════
#  2. 新 v4 信号扫描（使用路由器 + 过滤器 + 计划生成器）
# ═══════════════════════════════════════════════════════════

def scan_signals_v4(sym: str, market: dict) -> list:
    """

    新路由式信号扫描:
    1. 拉取K线 + 计算指标
    2. 路由交易类型 (route_trade_type)
    3. 执行过滤器 (position/BTC/overheat/sector)
    4. 生成交易计划 (build_trade_plan)
    
    返回 [(TradePlan, TradeType, Direction, score)]
    """

    try:
        df_5m = fetch_klines(None, sym, "5m", 60)
        df_1m = fetch_klines(None, sym, "1m", 30)
        if df_5m is None or df_1m is None or df_5m.empty or df_1m.empty:
            return []
    except Exception:
        return []

    df_5m = compute_indicators(df_5m)
    df_1m = compute_indicators(df_1m)

    last5 = df_5m.iloc[-1]
    last1 = df_1m.iloc[-1]

    closes_1m = [float(r["close"]) for _, r in df_1m.iterrows()]
    closes_5m = [float(r["close"]) for _, r in df_5m.iterrows()]
    price = float(last1["close"])
    atr5 = float(last5.get("atr", price * 0.005))
    atr1 = float(last1.get("atr", price * 0.002))

    d = SymbolData(
        symbol=sym,
        closes_1m=closes_1m,
        df_1m=df_1m,
        closes_5m=closes_5m,
        df_5m=df_5m,
        price=price,
        ema20_1m=calc_ema(closes_1m, 20),
        ema20_5m=calc_ema(closes_5m, 20),
        ema9_5=float(last5.get("ema9", price)),
        ema21_5=float(last5.get("ema21", price)),
        atr1=atr1,
        atr5=atr5,
        rsi5=float(last5.get("rsi", 50)),
        vol_ratio=float(last5.get("volume", 0)) / max(float(last5.get("vol_avg", 1)), 0.01),
        swing_low=float(last5.get("swing_low", price * 0.99)),
        swing_high=float(last5.get("swing_high", price * 1.01)),
        bias=market.get("bias", 0),
        regime=market.get("regime", "range"),
        heat=market.get("heat", "normal"),
    )

    # 路由
    trade_type, direction, reason = route_trade_type(d)
    if trade_type == TradeType.NO_TRADE or direction is None:
        return []

    # BTC 过滤 (14章)
    try:
        df_btc_1m = fetch_klines(None, "BTCUSDT", "1m", 25)
        df_btc_5m = fetch_klines(None, "BTCUSDT", "5m", 25)
        if df_btc_1m is not None and not df_btc_1m.empty and df_btc_5m is not None and not df_btc_5m.empty:
            btc_closes_1m = [float(r["close"]) for _, r in df_btc_1m.iterrows()]
            btc_highs_1m = [float(r["high"]) for _, r in df_btc_1m.iterrows()]
            btc_lows_1m = [float(r["low"]) for _, r in df_btc_1m.iterrows()]
            btc_closes_5m = [float(r["close"]) for _, r in df_btc_5m.iterrows()]

            atr_recent = _calc_btc_atr_recent(df_btc_1m, 5)
            atr_base = _calc_btc_atr_recent(df_btc_1m, 20)

            ok, reason = btc_dominance_filter(
                btc_closes_1m, btc_highs_1m, btc_lows_1m,
                btc_closes_5m, trade_type, direction,
                atr_recent, atr_base,
            )
            if not ok:
                return []
    except Exception as e:
        pass  # BTC数据失败不阻塞

    # BTC趋势反转检测（持续多/空头后首次破位降级）
    btc_risk_factor = 1.0
    try:
        from strategy.filters import btc_trend_flip_filter
        tf_ok, tf_factor, tf_reason = btc_trend_flip_filter(df_btc_5m, direction)
        if tf_factor < 1.0:
            btc_risk_factor = tf_factor
    except Exception:
        pass

    # 过热过滤 (11章)
    ok, heat_factor, h_reason = overheat_filter(df_1m, d.atr5, direction, trade_type)
    if not ok:
        return []

    # 位置过滤 (12章)
    ok, p_reason = position_filter(
        sym, direction, price, d.ema20_5m, d.atr5,
        d.swing_low, d.swing_high, d.atr1, trade_type,
    )
    if not ok:
        return []

    # ── 新增过滤器 (趋势/震荡双引擎) ──
    from strategy.position_utils import (
        calc_position_percentile, calc_vwap, extreme_position_penalty
    )
    from strategy.filters import (
        vwap_deviation_filter, position_percentile_filter,
        momentum_exhaustion_filter, range_middle_filter,
    )
    from strategy.market_state import get_state

    # 区间中部禁止 (11章)
    highs_1m = [float(r["high"]) for _, r in df_1m.iterrows()]
    lows_1m = [float(r["low"]) for _, r in df_1m.iterrows()]
    pct_data = calc_position_percentile(closes_1m, highs_1m, lows_1m)
    gs = get_state()
    regime_str = gs.regime.value if gs.updated else "CHOP"
    ok, rm_reason = range_middle_filter(pct_data, regime_str)
    if not ok:
        return []

    # VWAP偏离过滤 (8章)
    vwap_data = calc_vwap(df_1m)
    ok, vw_reason = vwap_deviation_filter(vwap_data, direction.value, trade_type.value, regime_str)
    if not ok:
        return []

    # 动量衰竭过滤 (9章)
    ok, me_reason = momentum_exhaustion_filter(df_1m, direction.value)
    if not ok:
        return []

    # 位置百分位过滤 + 极值惩罚 (5章)
    ok, pct_penalty = position_percentile_filter(pct_data, direction.value, trade_type.value)
    if not ok:
        return []

    # 计算评分 (方向分 + 位置分分离, 6章)
    raw_score = _calc_signal_score(d, trade_type, direction)
    if raw_score < 65:
        return []

    # 极值位置惩罚 + BTC加权
    score = int((raw_score + pct_penalty) * btc_risk_factor)
    if score < 65:
        return []

    # 通过所有检查, 构建计划入口
    return [(trade_type, direction, score, reason, btc_risk_factor)]


def _calc_signal_score(d: SymbolData, trade_type: TradeType, direction: Direction) -> float:
    """
简化的信号评分"""

    score = 60.0
    # 趋势加分
    if d.ema9_5 > d.ema21_5 and direction == Direction.LONG: score += 15
    if d.ema9_5 < d.ema21_5 and direction == Direction.SHORT: score += 15
    # RSI中性加分
    if 35 <= d.rsi5 <= 60: score += 8
    # 量能加分
    if d.vol_ratio > 1.3: score += 5
    # 动量型加分
    if trade_type in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND):
        score += 5
    return round(score, 1)


def _calc_btc_atr_recent(df_btc, period: int):
    """
快速计算BTC ATR"""

    if df_btc is None or len(df_btc) < period + 1:
        return 0
    trs = []
    for i in range(-period, 0):
        c = df_btc.iloc[i]
        pc = df_btc.iloc[i - 1]["close"]
        h, l = float(c["high"]), float(c["low"])
        trs.append(max(h - l, abs(h - float(pc)), abs(l - float(pc))))
    return sum(trs) / len(trs) if trs else 0


# ═══════════════════════════════════════════════════════════
#  3. 开仓（通过 TradePlan）
# ═══════════════════════════════════════════════════════════

def open_position_v4(trade_type: TradeType, direction: Direction, sym: str, score: float, btc_factor: float = 1.0):
    """
按 v4 流程开仓"""

    if sym in position_state:
        logger.info(f"  ⏭️ {sym} 已有持仓")
        return

    # 仓位上限检查（双重防竞态）
    if len(position_state) >= MAX_POSITIONS:
        logger.debug(f"  ⏭️ {sym} 已达仓位上限{MAX_POSITIONS}")
        return

    # BTC趋势反转 → 仓位减半
    global LEVERAGE
    eff_leverage = LEVERAGE
    if btc_factor < 1.0:
        eff_leverage = max(1, int(LEVERAGE * btc_factor))
        logger.info(f"  ⚠️ BTC趋势翻转风险, 杠杆{LEVERAGE}x→{eff_leverage}x")

    # 检查风控
    ok, risk_pct, reason = _check_risk_v4(trade_type, sym)
    if not ok:
        return

    # 拉一次完整数据用于构建计划
    try:
        df_5m = fetch_klines(None, sym, "5m", 60)
        df_1m = fetch_klines(None, sym, "1m", 30)
        if df_5m is None or df_1m is None or df_5m.empty or df_1m.empty:
            return
        df_5m = compute_indicators(df_5m)
        df_1m = compute_indicators(df_1m)
    except Exception:
        return

    # 构建TradePlan
    plan = build_trade_plan(
        sym, trade_type, direction,
        df_1m, df_5m,
        analyze_market(),  # 最新市场状态
        balance=_get_balance(),
        existing_positions=list(position_state.values()),
    )
    if plan is None:
        return

    # 对齐精度
    _load_precisions()
    qty = _align_qty(sym, plan.qty)
    if qty <= 0:
        return
    qty_str = ("%g" % qty).replace(",", "")

    side = "BUY" if direction == Direction.LONG else "SELL"
    try:
        _api("POST", "leverage", {"symbol": sym, "leverage": eff_leverage})
        order = _api("POST", "order", {
            "symbol": sym, "side": side,
            "type": "MARKET", "quantity": qty_str, "positionSide": direction.value,
        })
        fills = order.get("fills", [])
        actual_entry = float(fills[0]["price"]) if fills else plan.entry

        sl_a, tp1_a = _align_sltp(sym, plan.sl_hard, plan.tp1, direction.value)
        _, tp2_a = _align_sltp(sym, plan.sl_hard, plan.tp2, direction.value)

        position_state[sym] = {
            "side": direction.value, "entry": actual_entry,
            "sl_hard": sl_a, "sl_soft": plan.sl_soft,
            "tp1": tp1_a, "tp2": tp2_a, "tp3": plan.tp3,
            "r": plan.r_value, "initial_r": plan.r_value, "rr": plan.rr_to_tp1,
            "qty": qty, "qty_str": qty_str,
            "trade_type": trade_type.value, "score": score,
            "opened_at": time.time(), "last_check": time.time(),
            "entry_bars": 0, "hit_tp1": False,
            "reason": plan.reason,
        }

        logger.info(f"  ✅ [{trade_type.value}] {sym} {direction.value} @ {actual_entry:.4f}")
        logger.info(f"     SL_h={sl_a:.4f}  TP1={tp1_a:.4f}  TP2={tp2_a:.4f}")
        logger.info(f"     R={plan.r_value:.6f}  RR={plan.rr_to_tp1:.1f}  保证金={plan.risk_usdt:.2f}U({qty_str}张)")
        return True
    except Exception as e:
        logger.error(f"  ❌ 开仓失败: {e}")
        return False


def _check_risk_v4(trade_type: TradeType, sym: str) -> tuple[bool, float, str]:
    """
调用风险模块检查"""

    ok, reason = account_allows_trade()
    if not ok:
        logger.warning(f"  ⛔ {reason}")
        return False, 0, reason

    ok, reason = mode_allows_trade(trade_type)
    if not ok:
        logger.warning(f"  ⛔ {reason}")
        return False, 0, reason

    ok, reason = symbol_allows_trade(sym)
    if not ok:
        logger.warning(f"  ⛔ {reason}")
        return False, 0, reason

    risk_pct = 1.0  # base, 动态计算会在 build_trade_plan 里做
    return True, risk_pct, "ok"


def _get_balance() -> float:
    """
从风控状态获取余额"""

    try:
        with open(BASE / "v4_cooldown.json") as f:
            s = json.load(f)
        # 从交易状态获取
        from trader import _fapi_get
        acct = _fapi_get("account", {})
        if acct:
            for a in acct.get("assets", []):
                if a["asset"] == "USDT":
                    return float(a.get("walletBalance", 200))
    except Exception:
        pass
    return 200.0  # 默认


# ═══════════════════════════════════════════════════════════
#  4. 持仓管理（沿用 v3, 平滑迁移）
# ═══════════════════════════════════════════════════════════

def manage_positions(sym: str, price: float, heavy: bool = False):
    """
管理单个持仓 - 同v3"""

    pos = position_state.get(sym)
    if not pos:
        return

    side = pos["side"]
    entry = pos["entry"]
    sl_h = pos["sl_hard"]
    sl_s = pos.get("sl_soft", sl_h)
    tp1 = pos.get("tp1", 0)
    tp2 = pos.get("tp2", 0)
    r_val = pos.get("r", 0.01)
    trade_type = pos.get("trade_type", "standard")
    hit_tp1 = pos.get("hit_tp1", False)

    if side == "LONG":
        current_r = (price - entry) / r_val if r_val > 0 else 0
        hit_sl_h = price <= sl_h
        hit_sl_s = price <= sl_s
        hit_tp1_now = not hit_tp1 and tp1 > 0 and price >= tp1
        hit_tp2 = tp2 > 0 and price >= tp2
    else:
        current_r = (entry - price) / r_val if r_val > 0 else 0
        hit_sl_h = price >= sl_h
        hit_sl_s = price >= sl_s
        hit_tp1_now = not hit_tp1 and tp1 > 0 and price <= tp1
        hit_tp2 = tp2 > 0 and price <= tp2

    # 硬止损
    if hit_sl_h:
        logger.info(f"  🛑 硬止损 {sym} {side} entry={entry:.4f}→{price:.4f} R={current_r:.1f}")
        _close_position(sym, price, f"硬止损 R{current_r:.1f}")
        return
    # 软止损
    if hit_sl_s and current_r < 0:
        logger.info(f"  ⚡ 软止损 {sym} {side} entry={entry:.4f}→{price:.4f} R={current_r:.1f}")
        _close_position(sym, price, f"软止损 R{current_r:.1f}")
        return
    # TP2 全平
    if hit_tp2:
        logger.info(f"  🎯 TP2 {sym} {side} entry={entry:.4f}→{price:.4f} R={current_r:.1f}")
        _close_position(sym, price, f"TP2 R{current_r:.1f}")
        return
    # 动量快单/二次入场: 2分钟未达TP1直接退出
    if trade_type in ("momentum_scalp", "momentum_second_entry") and bars >= 2 and current_r < 0.4:
        logger.info(f"  ⏱ 动量2分钟未达TP1 {sym} {side} bars={bars} R={current_r:.1f}")
        _close_position(sym, price, f"动量2分钟未达TP1 R{current_r:.1f}")
        return
    # TP1 部分平仓
    if hit_tp1_now:
        pct = {"probe": 0.5, "standard": 0.4, "strong_trend": 0.25,
               "momentum_scalp": 0.5, "momentum_second_entry": 0.5,
               "failed_breakout_reversal": 0.5, "pullback_standard": 0.4,
               "breakout_retest": 0.3}.get(trade_type, 0.4)
        pos["hit_tp1"] = True
        # 如果剩余仓位太小(<8U保证金)，直接全平，释放仓位名额
        remaining_margin = abs(pos.get("qty", 0)) * price / LEVERAGE
        if remaining_margin < 8:
            logger.info(f"  🏁 TP1 {sym} 剩余保证金{remaining_margin:.1f}U<8U, 全平释放仓位")
            _close_position(sym, price, f"TP1+剩余过小 R{current_r:.1f}")
            return
        _reduce_position(sym, price, pct)
        if side == "LONG":
            pos["sl_hard"] = max(pos["sl_hard"], entry * 0.998)
        else:
            pos["sl_hard"] = min(pos["sl_hard"], entry * 1.002)
        logger.info(f"  🏁 TP1 {sym} 平{pct*100:.0f}% 止损移至entry")
    # 时间止损
    current_minute = int(time.time() // 60)
    last_candle = pos.get("last_candle_minute", 0)
    if current_minute != last_candle:
        pos["last_candle_minute"] = current_minute
        pos["entry_bars"] = pos.get("entry_bars", 0) + 1
    bars = pos["entry_bars"]
    # 时间止损使用初始R值计算（防止R值膨胀稀释亏损）
    initial_r = pos.get("initial_r", pos.get("r", 0.01))
    if initial_r > 0 and initial_r != r_val:
        time_r = (price - entry) / initial_r if side == "LONG" else (entry - price) / initial_r
    else:
        time_r = current_r
    timeout = {"probe": 5, "standard": 10, "strong_trend": 15,
               "momentum_scalp": 3, "momentum_second_entry": 3,
               "failed_breakout_reversal": 5, "pullback_standard": 5,
               "breakout_retest": 5}.get(trade_type, 5)
    if bars >= timeout and time_r < -0.3 and not pos.get("time_stopped", False):
        pos["time_stopped"] = True
        logger.info(f"  ⏰ 时间止损 {sym} {bars}m R={current_r:.1f} 全平")
        _close_position(sym, price, f"时间止损 {bars}m")
        return

    # TP1后30分钟未达TP2 → 平掉剩余仓位释放名额
    if pos.get("hit_tp1", False) and bars > 30 and time_r < 1.5:
        logger.info(f"  ⏳ TP1后30m未达TP2 {sym} {side} bars={bars} R={current_r:.1f} 平剩余")
        _close_position(sym, price, f"TP1超时未达TP2 R{current_r:.1f}")
        return

    # 移动止盈(heavy)
    if heavy and hit_tp1:
        try:
            df = fetch_klines(None, sym, "1m", 5)
            if df is not None and not df.empty:
                df = compute_indicators(df)
                last = df.iloc[-1]
                ema20_1m = float(last.get("sma20", float(last["close"])))
                atr_1m = float(last.get("atr", 0.001))
                if side == "LONG":
                    pos["sl_hard"] = max(pos["sl_hard"], ema20_1m - atr_1m * 0.8)
                else:
                    pos["sl_hard"] = min(pos["sl_hard"], ema20_1m + atr_1m * 0.8)
        except Exception:
            pass


def _query_exchange_position_qty(sym: str, position_side: str) -> float:
    """Return the absolute live position quantity for a hedge-mode side.

    Raises when Binance cannot be queried; callers must not treat query failure
    as an empty position.
    """
    rows = _api("GET", "positionRisk", {"symbol": sym})
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows or []:
        if row.get("symbol") != sym:
            continue
        row_side = row.get("positionSide", "BOTH")
        if row_side not in (position_side, "BOTH"):
            continue
        return abs(float(row.get("positionAmt", 0) or 0))
    return 0.0


def _close_position(sym: str, price: float, reason: str = "") -> bool:
    """Close a live position and update local state only after confirmation."""
    pos = position_state.get(sym)
    if not pos:
        return False

    try:
        _api("POST", "order", {
            "symbol": sym,
            "side": "SELL" if pos["side"] == "LONG" else "BUY",
            "type": "MARKET",
            "quantity": pos.get("qty_str", "0"),
            "positionSide": pos["side"],
        })
    except Exception as exc:
        logger.warning(f"  ⚠️ 平仓请求失败 {sym}: {exc}")
        pos["status"] = "CLOSE_PENDING"
        pos["close_error"] = str(exc)
        return False

    try:
        remaining = _query_exchange_position_qty(sym, pos["side"])
    except Exception as exc:
        logger.error(f"  ❌ 平仓后持仓确认失败 {sym}: {exc}")
        pos["status"] = "CLOSE_PENDING"
        pos["close_error"] = f"confirm_failed: {exc}"
        return False

    if remaining > 0:
        logger.warning(f"  ⚠️ 平仓未完全成交 {sym}: remaining={remaining}")
        pos["qty"] = remaining
        pos["qty_str"] = ("%g" % remaining).replace(",", "")
        pos["status"] = "CLOSE_PENDING"
        return False

    entry = pos["entry"]
    qty = abs(pos.get("qty", 0))
    pnl = ((price - entry) if pos["side"] == "LONG" else (entry - price)) * qty
    trade_type_str = pos.get("trade_type", "standard")

    position_state.pop(sym, None)
    logger.info(f"  ✅ 平仓确认 {sym} {reason}  PnL={pnl:+.4f}U")
    update_account_result(pnl)
    won = pnl > 0
    record_symbol_result(sym, won)
    try:
        tt_enum = TradeType(trade_type_str)
    except ValueError:
        tt_enum = TradeType.PULLBACK_STANDARD
    record_mode_result(tt_enum, won, pos.get("r", 0))
    return True


def _reduce_position(sym: str, price: float, pct: float) -> bool:
    """Reduce a live position and synchronize the remaining quantity."""
    pos = position_state.get(sym)
    if not pos:
        return False
    old_qty = abs(pos.get("qty", 0))
    if old_qty <= 0:
        return False

    _load_precisions()
    reduce_qty = _align_qty(sym, old_qty * pct)
    if reduce_qty <= 0:
        logger.info(f"  🔄 无法按精度减仓 {sym}，改为真实全平")
        return _close_position(sym, price, "REDUCE_QTY_BELOW_PRECISION")

    qty_str = ("%g" % reduce_qty).replace(",", "")
    try:
        _api("POST", "order", {
            "symbol": sym,
            "side": "SELL" if pos["side"] == "LONG" else "BUY",
            "type": "MARKET",
            "quantity": qty_str,
            "positionSide": pos["side"],
        })
    except Exception as exc:
        logger.warning(f"  ⚠️ 减仓请求失败 {sym}: {exc}")
        pos["status"] = "REDUCE_PENDING"
        pos["reduce_error"] = str(exc)
        return False

    try:
        remaining = _query_exchange_position_qty(sym, pos["side"])
    except Exception as exc:
        logger.error(f"  ❌ 减仓后持仓确认失败 {sym}: {exc}")
        pos["status"] = "REDUCE_PENDING"
        pos["reduce_error"] = f"confirm_failed: {exc}"
        return False

    if remaining <= 0:
        position_state.pop(sym, None)
        logger.info(f"  ✅ 减仓后仓位归零，清理 {sym}")
        return True

    pos["qty"] = remaining
    pos["qty_str"] = ("%g" % remaining).replace(",", "")
    pos["status"] = "active"
    logger.info(f"  🔻 减仓确认 {sym} {pct*100:.0f}% remaining={remaining:.4f}")
    return True


# ═══════════════════════════════════════════════════════════
#  5. 持仓同步 + 全市场扫描 (同v3)
# ═══════════════════════════════════════════════════════════

def recalc_position_sltp():
    """
同v3"""

    for sym, pos in list(position_state.items()):
        try:
            df = fetch_klines(None, sym, "5m", 60)
            if df is None or df.empty or len(df) < 30: continue
            df = compute_indicators(df)
            last = df.iloc[-1]
            close = float(last["close"])
            atr = float(last.get("atr", close * 0.005))
            swing_low = float(last.get("swing_low", close * 0.99))
            swing_high = float(last.get("swing_high", close * 1.01))
            side = pos["side"]
            entry = pos["entry"]
            if side == "LONG":
                sl_h = min(swing_low - atr * 0.5, close * 0.993)
                sl_h = max(sl_h, close * 0.988)
                r = abs(entry - sl_h)
                tp1 = entry + r * 0.8
                tp2 = entry + r * 1.8
            else:
                sl_h = max(swing_high + atr * 0.5, close * 1.007)
                sl_h = min(sl_h, close * 1.012)
                r = abs(sl_h - entry)
                tp1 = entry - r * 0.8
                tp2 = entry - r * 1.8
            sl_a, tp1_a = _align_sltp(sym, sl_h, tp1, side)
            _, tp2_a = _align_sltp(sym, sl_h, tp2, side)
            old_sl = pos.get("sl_hard", 0)
            changed = abs(sl_a - old_sl) > atr * 0.1 if old_sl else True
            if changed:
                pos["sl_hard"] = sl_a
                pos["sl_soft"] = sl_a
                pos["tp1"] = tp1_a
                pos["tp2"] = tp2_a
                pos["r"] = abs(entry - sl_a)
                logger.info(f"  📐 更新{sym} R值SL/TP: {old_sl:.4f}→{sl_a:.4f}  TP1={tp1_a:.4f}  R={pos['r']:.6f}")
        except Exception as e:
            logger.debug(f"重算{sym} SL/TP失败: {e}")


def sync_positions():
    """
同v3"""

    import requests as rq, hmac, hashlib, urllib.parse
    prox = {'http': PROXY, 'https': PROXY}
    ts = int(time.time() * 1000)
    p = {'timestamp': str(ts), 'recvWindow': '10000'}
    q = urllib.parse.urlencode(sorted(p.items()))
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    r = rq.get(f'https://fapi.binance.com/fapi/v2/positionRisk?{q}&signature={sig}',
               headers={'X-MBX-APIKEY': API_KEY}, proxies=prox, timeout=10)
    if r.status_code != 200: return []
    positions = [p for p in r.json() if abs(float(p['positionAmt'])) > 0]

    # 取消条件单
    for pos in positions:
        sym = pos['symbol']
        try:
            p2 = {'symbol': sym, 'timestamp': int(time.time()*1000), 'recvWindow': 10000}
            q2 = urllib.parse.urlencode(sorted(p2.items()))
            sig2 = hmac.new(API_SECRET.encode(), q2.encode(), hashlib.sha256).hexdigest()
            r2 = rq.get(f'https://fapi.binance.com/fapi/v1/allAlgoOrders?{q2}&signature={sig2}',
                        headers={'X-MBX-APIKEY': API_KEY}, proxies=prox, timeout=10)
            if r2.status_code == 200:
                for a in r2.json():
                    if a.get('algoStatus') in ('NEW', 'WORKING'):
                        try:
                            p3 = {'symbol': sym, 'algoId': str(a['algoId']), 'timestamp': int(time.time()*1000), 'recvWindow': 10000}
                            q3 = urllib.parse.urlencode(sorted(p3.items()))
                            sig3 = hmac.new(API_SECRET.encode(), q3.encode(), hashlib.sha256).hexdigest()
                            rq.delete(f'https://fapi.binance.com/fapi/v1/algoOrder?{q3}&signature={sig3}',
                                      headers={'X-MBX-APIKEY': API_KEY}, proxies=prox, timeout=5)
                            time.sleep(0.2)
                        except Exception: pass
        except Exception: pass

    for pos in positions:
        sym = pos['symbol']
        side = pos['positionSide']
        entry = float(pos['entryPrice'])
        qty_amt = abs(float(pos['positionAmt']))
        from config import STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT, DEFAULT_LEVERAGE
        sl_pct = STOP_LOSS_PERCENT / DEFAULT_LEVERAGE / 100
        tp_pct = TAKE_PROFIT_PERCENT / DEFAULT_LEVERAGE / 100
        if side == 'LONG':
            sl = entry * (1 - sl_pct); tp = entry * (1 + tp_pct)
        else:
            sl = entry * (1 + sl_pct); tp = entry * (1 - tp_pct)
        sl_a, tp_a = _align_sltp(sym, sl, tp, side)
        qty_str = ('%g' % qty_amt).replace(',', '')
        position_state[sym] = {
            'side': side, 'entry': entry, 'sl_hard': sl_a, 'tp1': tp_a, 'tp2': 0,
            'r': abs(entry - sl_a), 'rr': 1.5, 'qty': qty_amt, 'qty_str': qty_str,
            'trade_type': 'standard', 'score': 0, 'opened_at': time.time(),
            'last_check': time.time(), 'entry_bars': 0, 'hit_tp1': False,
            'reason': '持仓同步', 'sl_soft': sl_a,
            'time_stopped': True,
        }
        logger.info(f"  🔄 同步: {sym:12s} {side:6s} entry={entry:.4f} sl={sl_a:.4f} tp={tp_a:.4f}")
    return [p['symbol'] for p in positions]


def scan_market_broad() -> list:
    """
15min全市场扫描, 同v3"""

    from concurrent.futures import ThreadPoolExecutor, as_completed
    btc_env = get_btc_environment()
    bias = btc_env.get("bias", 0)
    tickers = fetch_all_tickers()
    if not tickers: return []
    candidates = [s for s in tickers if s.endswith("USDT") and (tickers[s].get("volume24h", 0) or 0) > 500000]
    candidates = sorted(candidates, key=lambda s: tickers[s].get("volume24h", 0), reverse=True)[:100]

    def score_one(sym):
        try:
            df = fetch_klines(None, sym, "5m", 60)
            if df is None or df.empty or len(df) < 30: return None
            df = compute_indicators(df)
            last = df.iloc[-1]
            close = float(last["close"])
            ema9 = float(last.get("ema9", close))
            ema21 = float(last.get("ema21", close))
            rsi = float(last.get("rsi", 50))
            vol_ratio = float(last.get("volume", 0)) / max(float(last.get("vol_avg", 1)), 0.01)
            score = 50
            if ema9 > ema21: score += 15
            if ema9 * 0.995 <= close <= ema9 * 1.005: score += 10
            if 35 <= rsi <= 60: score += 8
            if vol_ratio > 1.3: score += 5
            if bias > 0 and ema9 > ema21: score += 5
            if bias < 0 and ema9 < ema21: score += 5
            return {"sym": sym, "score": round(score, 1)}
        except Exception: return None

    # 扫前100个币评分→Top30进观测区
    results = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(score_one, s): s for s in candidates[:100]}
        for f in as_completed(futures):
            try:
                r = f.result(timeout=15)
                if r: results.append(r)
            except Exception: pass
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:30]


def update_watch_zone(scored: list):
    """
同v3"""

    global watch_zone
    now = time.time()
    cutoff = now - 1800
    expired = [s for s in watch_zone if watch_zone[s].get("added_at", 0) < cutoff and s not in position_state]
    for s in expired: del watch_zone[s]
    for s in list(watch_zone.keys()):
        if s in position_state: del watch_zone[s]
    count = len(watch_zone)
    for r in scored:
        if r["score"] < 65: continue
        if r["sym"] in watch_zone or r["sym"] in position_state: continue
        if count >= 30: break
        watch_zone[r["sym"]] = {"added_at": now, "score": r["score"]}
        count += 1
    logger.info(f"  观测区: {len(watch_zone)}个币")


# ═══════════════════════════════════════════════════════════
#  6. 主循环
# ═══════════════════════════════════════════════════════════

def main():
    global running
    logger.info("=" * 60)
    logger.info("🚀 实时量化交易系统 v4")
    logger.info("   五种交易类型: 回踩标准 / 突破回踩 / 动量快单 / 二次入场 / 假突破反打")
    logger.info("   实时监控: 持仓秒级SL/TP + 15min全市场扫描 + 观测区")
    logger.info("   风控: 三止损 + 连亏保护 + 冷却 + 模式反馈")
    logger.info("   杠杆: 5x 统一")
    logger.info("=" * 60)

    existing = sync_positions()
    logger.info(f"同步完成: {len(existing)}个现有持仓")
    if existing:
        recalc_position_sltp()

    market = analyze_market()
    logger.info(f"市场: {market['regime']}  热度:{market['heat']}  BTCbias:{market['bias']:+d}")
    scored = scan_market_broad()
    if scored:
        update_watch_zone(scored)
        top5 = [s['sym'] + '(' + str(s['score']) + ')' for s in scored[:5]]
        logger.info('  Top: ' + '  '.join(top5))

    tick = 0
    heavy_tick = 0
    broad_tick = 0

    while running:
        cycle_start = time.time()
        tick += 1

        # ── 15min 全市场扫描 ──
        broad_tick += 1
        if broad_tick >= 900:
            broad_tick = 0
            market = analyze_market()
            logger.info(f"🔍 全市场扫描（每15分钟）...")
            scored = scan_market_broad()
            if scored:
                update_watch_zone(scored)

        # ── 1s: 价格轮询 + 持仓管理 ──
        poll_syms = list(set(list(position_state.keys()) + list(watch_zone.keys())))
        if not poll_syms:
            poll_syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

        prices = _fetch_prices(poll_syms)
        for sym in poll_syms:
            p = prices.get(sym)
            if p:
                price_cache[sym] = p
                if sym in position_state:
                    manage_positions(sym, p)

        # ── 60s: 信号扫描 + 移动止盈 + 状态报告 ──
        heavy_tick += 1
        if heavy_tick >= 60:
            heavy_tick = 0

            # K线全失败时触发代理节点检查
            try:
                df = fetch_klines(None, "BTCUSDT", "1m", 5)
                if df is None or df.empty:
                    raise Exception("无数据")
            except Exception:
                logger.warning("  🌐 K线全部失败，触发代理节点检查...")
                try:
                    from proxy_guard import ensure_connection
                    if ensure_connection():
                        logger.info("  ✅ 代理已恢复")
                    else:
                        logger.warning("  ⚠️ 代理仍不可用，将跳过本轮信号扫描")
                except Exception as pe:
                    logger.error(f"  代理检查异常: {pe}")

            # 全局市场状态更新（优先于信号扫描）
            try:
                df_btc_state = fetch_klines(None, "BTCUSDT", "5m", 60)
                if df_btc_state is not None and not df_btc_state.empty:
                    btc_price = price_cache.get("BTCUSDT", 0) or float(df_btc_state.iloc[-1]["close"])
                    update_market_state(df_btc_state, btc_price)
            except Exception:
                pass
            ms = market_state_summary()
            logger.info(f"  {ms}")

            recalc_position_sltp()
            for sym in list(position_state.keys()):
                p = price_cache.get(sym, 0)
                if p > 0:
                    manage_positions(sym, p, heavy=True)

            # v4 信号扫描（全观测区，10线程并行）
            market = analyze_market()
            wz_syms = [s for s in watch_zone if s not in position_state]
            if wz_syms and len(position_state) < MAX_POSITIONS:
                signals = []
                with ThreadPoolExecutor(max_workers=10) as ex:
                    fut_map = {ex.submit(scan_signals_v4, sym, market): sym for sym in wz_syms}
                    for f in as_completed(fut_map):
                        sym = fut_map[f]
                        try:
                            for result in f.result(timeout=15):
                                trade_type, direction, score, reason, btc_factor = result
                                signals.append((sym, trade_type, direction, score, reason, btc_factor))
                        except Exception as e:
                            logger.debug(f"  跳过{sym}: {e}")

                # 全局市场状态过滤
                from strategy.market_state import get_global_permission
                filtered = []
                for sig in signals:
                    sym, trade_type, direction, score, reason, btc_factor = sig
                    allowed, global_factor = get_global_permission(direction.value, trade_type.value)
                    if allowed:
                        # 合并全局因子到btc_factor
                        combined = btc_factor * global_factor
                        filtered.append((sym, trade_type, direction, score, reason, combined))
                    else:
                        logger.debug(f"  ⛔ 全局禁止 {sym} {direction.value} (f={global_factor})")
                signals = filtered

                signals.sort(key=lambda x: x[3], reverse=True)
                logger.info(f"  📊 信号扫描: {len(wz_syms)}个币 → {len(signals)}个信号  Top评分={signals[0][3] if signals else '无'}")

                for sym, trade_type, direction, score, reason, btc_factor in signals[:3]:
                    if sym in position_state:
                        continue
                    if open_position_v4(trade_type, direction, sym, score, btc_factor):
                        if sym in watch_zone:
                            del watch_zone[sym]

            # 清理观测区
            cutoff = time.time() - 1800
            for s in list(watch_zone.keys()):
                if s in position_state:
                    del watch_zone[s]
                elif watch_zone[s].get("added_at", 0) < cutoff:
                    del watch_zone[s]

            # 状态报告
            pos_details = []
            for s, p in list(position_state.items())[:5]:
                cur = price_cache.get(s, p['entry'])
                if p['side'] == 'LONG':
                    pnl_pct = (cur - p['entry']) / p['entry'] * LEVERAGE * 100
                else:
                    pnl_pct = (p['entry'] - cur) / p['entry'] * LEVERAGE * 100
                icon = '✅' if pnl_pct > 0 else '❌'
                pos_details.append(f"{s} {icon} {pnl_pct:+.1f}%")
            logger.info(f"📊 持仓:{len(position_state)}/{MAX_POSITIONS}  观测:{len(watch_zone)}")

        elapsed = time.time() - cycle_start
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        if tick % 300 == 0:
            logger.info(f"💓 心跳 持仓{len(position_state)} 观测{len(watch_zone)}")

    logger.info("👋 系统已停止")


def _fetch_prices(symbols: list) -> dict:
    """
获取价格"""

    import requests as rq
    prox = {'http': PROXY, 'https': PROXY}
    result = {}
    try:
        r = rq.get("https://fapi.binance.com/fapi/v1/ticker/price", proxies=prox, timeout=8)
        if r.status_code == 200:
            for d in r.json():
                result[d["symbol"]] = float(d["price"])
    except Exception:
        pass
    return result


if __name__ == "__main__":
    main()

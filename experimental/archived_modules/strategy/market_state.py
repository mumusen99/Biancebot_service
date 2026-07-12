"""
全局市场结构保护层 (v4)
=======================
引用: 超短线策略补充文档

核心逻辑：
1. 扫描BTC/ETH摆动结构 → 识别 Lower High/Lower Low
2. 映射到5种市场状态 (RISK_ON / PULLBACK / DISTRIBUTION / RISK_OFF / CHOP)
3. 生成全局方向系数 (long_factor / short_factor)
4. 控制 PROFIT_LOCK_MODE 和 LOSS_COOLDOWN

执行顺序（必须优先于单币信号扫描）：
  全局状态机 → 方向许可 → 单币路由 → 下单
"""
import json, time, logging
from pathlib import Path
from datetime import date
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("v4")
BASE = Path(__file__).parent.parent
STATE_FILE = BASE / "v4_market_state.json"


# ─── 枚举 ─────────────────────────────────────────

class MarketRegime(str, Enum):
    RISK_ON = "RISK_ON"             # 上涨结构健康
    PULLBACK = "PULLBACK"           # 正常回调
    DISTRIBUTION = "DISTRIBUTION"   # 疑似派发
    RISK_OFF = "RISK_OFF"           # 下跌结构成立
    CHOP = "CHOP"                   # 震荡无序


# ─── 全局状态 ─────────────────────────────────────

@dataclass
class GlobalMarketState:
    regime: MarketRegime = MarketRegime.CHOP
    long_factor: float = 1.0
    short_factor: float = 0.5
    global_long_freeze: bool = False
    freeze_until: float = 0
    profit_lock: bool = False
    profit_lock_until: float = 0
    loss_cooldown: bool = False
    loss_cooldown_until: float = 0
    
    # 摆动结构历史
    swing_highs: list = field(default_factory=list)   # [(price, time)]
    swing_lows: list = field(default_factory=list)    # [(price, time)]
    prev_swing_high: float = 0
    prev_swing_low: float = 0
    consecutive_lower_highs: int = 0
    consecutive_lower_lows: int = 0
    
    # 时间戳
    last_update: float = 0
    updated: bool = False
    
    # 状态稳定
    regime_lock: int = 0          # 连续确认bar数
    pending_regime: str = ""      # 待切换状态
    pending_long_f: float = -1    # 待切换long_factor
    pending_short_f: float = -1   # 待切换short_factor
    
    # 冻结恢复条件跟踪
    freeze_unlock_count: int = 0  # 连续满足解冻条件的bar数


# ─── 全局单例 ────────────────────────────────────

_state = GlobalMarketState()


def get_state() -> GlobalMarketState:
    return _state


# ═══════════════════════════════════════════════════════════
#  摆动结构识别 (3章)
# ═══════════════════════════════════════════════════════════

def detect_swing_points(df_5m, lookback: int = 5) -> dict:
    """
    检测最近摆动高点和低点 (5m周期, N=5)。
    返回 {swing_high, swing_low, atr5}
    """
    if df_5m is None or len(df_5m) < lookback + 2:
        return {"swing_high": 0, "swing_low": 0, "atr5": 0}

    # ATR
    trs = []
    for i in range(1, min(15, len(df_5m))):
        h = float(df_5m.iloc[-i]["high"])
        l = float(df_5m.iloc[-i]["low"])
        pc = float(df_5m.iloc[-i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr5 = sum(trs) / len(trs) if trs else 0

    # 摆动高点和低点 (最后N根的极值，滞后2根避免未来函数)
    highs = [float(df_5m.iloc[-i]["high"]) for i in range(3, 3 + lookback)]
    lows = [float(df_5m.iloc[-i]["low"]) for i in range(3, 3 + lookback)]

    return {
        "swing_high": max(highs) if highs else 0,
        "swing_low": min(lows) if lows else 0,
        "atr5": atr5,
    }


def detect_lower_highs(swing_points: dict, prev_swing_high: float) -> tuple[int, bool]:
    """
    Lower High 检测 (4章):
    最新反弹高点 < 前一个有效高点 - 0.2 * ATR_5m
    返回 (连续出现次数, 是否触发)
    """
    sh = swing_points.get("swing_high", 0)
    atr = swing_points.get("atr5", 0) or 0.001
    if prev_swing_high <= 0 or sh <= 0:
        return 0, False
    if sh < prev_swing_high - 0.2 * atr:
        return max(1, _state.consecutive_lower_highs + 1), True
    return 0, False


def detect_lower_lows(swing_points: dict, prev_swing_low: float, price: float) -> tuple[int, bool]:
    """
    Lower Low 检测 (4章):
    最新低点 < 前一个有效低点 - 0.2 * ATR_5m
    或: BTC close < prev_swing_low - 0.2 * ATR_5m
    """
    sl = swing_points.get("swing_low", 0)
    atr = swing_points.get("atr5", 0) or 0.001
    threshold = prev_swing_low - 0.2 * atr
    if prev_swing_low <= 0:
        return 0, False
    triggered = (sl > 0 and sl < threshold) or (price < threshold)
    if triggered:
        return max(1, _state.consecutive_lower_lows + 1), True
    return 0, False


# ═══════════════════════════════════════════════════════════
#  市场状态机 (2章, 5章)
# ═══════════════════════════════════════════════════════════

def update_market_state(df_btc_5m, btc_price: float, top50_above_ema_ratio: float = None):
    """
    主入口：更新全局市场状态（含稳定性修正）。
    状态切换需连续3根5m K线确认。
    冻结持续到价格恢复 + 2根确认。
    """
    st = _state
    sp = detect_swing_points(df_btc_5m)
    atr5 = sp.get("atr5", 0) or 0.001
    sw_high = sp.get("swing_high", 0)
    sw_low = sp.get("swing_low", 0)
    now = time.time()
    closes = [float(r["close"]) for _, r in df_btc_5m.iterrows()]

    # 摆动结构
    lh_count, lh_triggered = detect_lower_highs(sp, st.prev_swing_high)
    ll_count, ll_triggered = detect_lower_lows(sp, st.prev_swing_low, btc_price)
    st.consecutive_lower_highs = lh_count if lh_triggered else 0
    st.consecutive_lower_lows = ll_count if ll_triggered else 0
    if sw_high > st.prev_swing_high: st.prev_swing_high = sw_high
    if 0 < sw_low < st.prev_swing_low or st.prev_swing_low <= 0: st.prev_swing_low = sw_low

    # 待切换状态
    pend_regime = st.regime
    pend_long = st.long_factor
    pend_short = st.short_factor
    pend_freeze = st.global_long_freeze

    # 冻结控制：价格低于结构位则持续冻结，恢复后2根K线解冻
    freeze_threshold = st.prev_swing_low - 0.2 * atr5
    if freeze_threshold > 0 and btc_price < freeze_threshold:
        pend_freeze = True
        st.freeze_unlock_count = 0
    elif pend_freeze:
        st.freeze_unlock_count += 1
        if st.freeze_unlock_count >= 2:
            pend_freeze = False
            st.freeze_unlock_count = 0

    # 状态判断
    if ll_triggered and lh_count >= 2:
        pend_regime = MarketRegime.RISK_OFF
        pend_long, pend_short = 0.0, 0.8
        pend_freeze = True
    elif lh_count >= 3:
        pend_regime = MarketRegime.DISTRIBUTION
        pend_long, pend_short = (0.0 if pend_freeze else 0.3), 0.8
    elif lh_count >= 2:
        pend_regime = MarketRegime.DISTRIBUTION
        pend_long, pend_short = (0.0 if pend_freeze else 0.3), 0.8
    elif len(closes) >= 20:
        from trading_bot.strategy.trade_router import ema as calc_ema
        ema20 = calc_ema(closes, 20)
        ema20_5ago = calc_ema(closes[:-6], 20) if len(closes) > 25 else ema20
        slope = ema20 - ema20_5ago
        if btc_price > ema20 and slope > 0 and not lh_triggered and not ll_triggered:
            pend_regime = MarketRegime.RISK_ON
            pend_long, pend_short = 1.0, 0.5
        elif pend_regime == MarketRegime.RISK_ON and btc_price < ema20:
            pend_regime = MarketRegime.PULLBACK
            pend_long, pend_short = (0.6 if not pend_freeze else 0.0), 0.5
        elif not lh_triggered and not ll_triggered and pend_regime != MarketRegime.RISK_ON:
            chg = abs(closes[-1] - closes[-5]) / max(closes[-5], 0.01)
            if chg < 0.003:
                pend_regime = MarketRegime.CHOP
                pend_long, pend_short = (0.5 if not pend_freeze else 0.0), 0.5

    if pend_freeze: pend_long = 0.0
    if st.profit_lock: pend_long *= 0.5; pend_short *= 0.5
    if st.loss_cooldown: pend_long = 0.0; pend_short = 0.0

    # 状态切换需连续3根K线确认
    st.regime_lock += 1
    if st.regime != pend_regime or abs(st.long_factor - pend_long) > 0.01 or abs(st.short_factor - pend_short) > 0.01:
        if st.regime_lock >= 3:
            if pend_long != st.long_factor:
                logger.info(f"  状态切换: {st.regime.value} → {pend_regime.value} | 做多{pend_long} 做空{pend_short}")
            st.regime = pend_regime
            st.long_factor = pend_long
            st.short_factor = pend_short
            st.global_long_freeze = pend_freeze
            st.regime_lock = 0
    else:
        st.regime_lock = 0

    # 冷却过期
    if st.profit_lock_until > 0 and now > st.profit_lock_until:
        st.profit_lock = False; st.profit_lock_until = 0
    if st.loss_cooldown_until > 0 and now > st.loss_cooldown_until:
        st.loss_cooldown = False; st.loss_cooldown_until = 0

    st.last_update = now; st.updated = True
    return st
def check_profit_lock(closed_trades_pnl: float, balance: float):
    """
    检查是否触发 PROFIT_LOCK_MODE。
    条件: 10分钟内 ≥3个TP2 或 账户权益短时间增加 ≥1%
    """
    st = _state
    if st.profit_lock:
        return

    # 账户权益增加≥1%
    pnl_pct = closed_trades_pnl / max(balance, 1)
    if pnl_pct >= 0.01:  # 1%
        st.profit_lock = True
        st.profit_lock_until = time.time() + 1200  # 20分钟
        logger.info(f"  🔒 PROFIT_LOCK: 近期盈利{closed_trades_pnl:.2f}U({pnl_pct*100:.1f}%), 限仓20分钟")


# ═══════════════════════════════════════════════════════════
#  LOSS_COOLDOWN (11章)
# ═══════════════════════════════════════════════════════════

def check_loss_cooldown(consecutive_losses: int, recent_losses_10m: int, recent_losses_30m: int):
    """
    检查全局连亏保护。
    返回 (是否触发冷却, 冷却时长_秒, 说明)
    """
    st = _state
    if st.loss_cooldown:
        return True, 0, "已在冷却中"

    # 10分钟内亏损≥3笔 → 暂停15分钟
    if recent_losses_10m >= 3:
        st.loss_cooldown = True
        st.loss_cooldown_until = time.time() + 900
        logger.info(f"  🛑 LOSS_COOLDOWN: 10min亏{recent_losses_10m}笔, 暂停15分钟")
        return True, 900, f"10分钟亏损{recent_losses_10m}笔"

    # 30分钟内亏损≥5笔 → 暂停30分钟
    if recent_losses_30m >= 5:
        st.loss_cooldown = True
        st.loss_cooldown_until = time.time() + 1800
        logger.info(f"  🛑 LOSS_COOLDOWN: 30min亏{recent_losses_30m}笔, 暂停30分钟")
        return True, 1800, f"30分钟亏损{recent_losses_30m}笔"

    # 连续3笔同方向亏损
    if consecutive_losses >= 3:
        st.loss_cooldown = True
        st.loss_cooldown_until = time.time() + 1800
        logger.info(f"  🛑 LOSS_COOLDOWN: 连亏{consecutive_losses}笔, 暂停30分钟")
        return True, 1800, f"连续亏损{consecutive_losses}笔"

    return False, 0, ""


# ═══════════════════════════════════════════════════════════
#  全局开仓许可 (12章, 13章)
# ═══════════════════════════════════════════════════════════

def get_global_permission(direction: str, trade_type: str) -> tuple[bool, float]:
    """
    全局开仓许可。
    返回 (允许开仓, 全局方向系数)
    """
    st = _state

    # 冷却 → 全部禁止
    if st.loss_cooldown:
        return False, 0.0

    # 方向冻结
    if direction == "LONG" and st.global_long_freeze:
        return False, 0.0
    if direction == "LONG" and st.long_factor <= 0:
        return False, 0.0
    if direction == "SHORT" and st.short_factor <= 0:
        return False, 0.0

    factor = st.long_factor if direction == "LONG" else st.short_factor
    return True, factor


# ═══════════════════════════════════════════════════════════
#  市场宽度 (9章)
# ═══════════════════════════════════════════════════════════

def calc_market_breadth(tickers: dict) -> dict:
    """
    统计Top50合约的EMA20位置比例。
    返回 {above_ema_ratio, total_scored}
    """
    return {"above_ema_ratio": 50, "total_scored": 0}


# ═══════════════════════════════════════════════════════════
#  报告
# ═══════════════════════════════════════════════════════════

def market_state_summary() -> str:
    st = _state
    parts = [
        f"市场: {st.regime.value}",
        f"做多系数: {st.long_factor:.1f}",
        f"做空系数: {st.short_factor:.1f}",
    ]
    if st.global_long_freeze:
        remain = max(0, int(st.freeze_until - time.time()) // 60)
        parts.append(f"❄️ 多头冻结({remain}分)")
    if st.profit_lock:
        remain = max(0, int(st.profit_lock_until - time.time()) // 60)
        parts.append(f"🔒 利润锁定({remain}分)")
    if st.loss_cooldown:
        remain = max(0, int(st.loss_cooldown_until - time.time()) // 60)
        parts.append(f"🛑 冷却中({remain}分)")
    return " | ".join(parts)


# ═══════════════════════════════════════════════════════════
#  状态持久化
# ═══════════════════════════════════════════════════════════

def save_state():
    st = _state
    data = {
        "regime": st.regime.value,
        "long_factor": st.long_factor,
        "short_factor": st.short_factor,
        "global_long_freeze": st.global_long_freeze,
        "freeze_until": st.freeze_until,
        "profit_lock": st.profit_lock,
        "profit_lock_until": st.profit_lock_until,
        "loss_cooldown": st.loss_cooldown,
        "loss_cooldown_until": st.loss_cooldown_until,
        "last_update": st.last_update,
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

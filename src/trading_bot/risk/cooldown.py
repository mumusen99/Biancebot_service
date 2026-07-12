"""
冷却系统 (v4)
==============
24章: 总账户风控 + 单币种冷却 + 模式冷却
"""
import json, time, logging
from pathlib import Path
from datetime import date
from trading_bot.strategy.trade_router import TradeType

logger = logging.getLogger("v3")
BASE = Path(__file__).parent.parent
COOLDOWN_FILE = BASE / "v4_cooldown.json"


def _load() -> dict:
    try:
        with open(COOLDOWN_FILE) as f:
            s = json.load(f)
        if s.get("date") == str(date.today()):
            return s
    except Exception:
        pass
    return {
        "date": str(date.today()),
        "symbol": {},       # {sym: {"losses": N, "cooldown_until": ts}}
        "mode": {},         # {trade_type: {"losses": N, "cooldown_until": ts, "last20_win_rate": None, "last20_avg_r": None}}
        "account": {        # 账户级
            "consecutive_losses": 0,
            "daily_pnl": 0.0,
            "trades_today": 0,
        },
        "symbol_cooldowns": {},  # {sym: cooldown_until_ts}
        "mode_cooldowns": {},
    }


def _save(s):
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ─── 账户级风控 ───────────────────────────────────

def account_allows_trade() -> tuple[bool, str]:
    s = _load()
    a = s["account"]
    if a["consecutive_losses"] >= 3:
        return False, f"连续亏损{a['consecutive_losses']}单, 暂停30-60分钟"
    if a["daily_pnl"] <= -5.0:
        return False, f"日亏损{a['daily_pnl']:.1f}U > 5U, 强制停止"
    if a["daily_pnl"] <= -3.0:
        return False, f"日亏损{a['daily_pnl']:.1f}U > 3U, 停止开新仓"
    return True, "ok"


def account_risk_multiplier() -> float:
    """连亏惩罚系数 (24.1 / 19.3)"""
    s = _load()
    c = s["account"]["consecutive_losses"]
    factors = {0: 1.0, 1: 0.7, 2: 0.5}
    return factors.get(c, 0.0)


def update_account_result(pnl: float):
    s = _load()
    a = s["account"]
    a["trades_today"] += 1
    a["daily_pnl"] = round(a["daily_pnl"] + pnl, 4)
    if pnl < 0:
        a["consecutive_losses"] += 1
    else:
        a["consecutive_losses"] = 0
    _save(s)


# ─── 单币种冷却 ───────────────────────────────────

def symbol_allows_trade(sym: str) -> tuple[bool, str]:
    """返回 (允许, 原因)"""
    s = _load()
    cooldowns = s.get("symbol_cooldowns", {})
    until = cooldowns.get(sym, 0)
    if time.time() < until:
        remaining = int(until - time.time())
        return False, f"{sym} 冷却中, 剩余{remaining//60}分"
    return True, "ok"


def symbol_risk_multiplier(sym: str) -> float:
    """单币连亏系数 (24.2)"""
    s = _load()
    entry = s.get("symbol", {}).get(sym, {})
    losses = entry.get("losses", 0)
    if losses >= 2:
        return 0.0  # 连续亏2次冷却1小时, 函数层面返回0, 实际由冷却队列控制
    if losses >= 1:
        return 0.5
    return 1.0


def record_symbol_result(sym: str, won: bool):
    s = _load()
    entry = s["symbol"].setdefault(sym, {"losses": 0})
    cooldowns = s.setdefault("symbol_cooldowns", {})

    if won:
        entry["losses"] = 0  # 盈利重置
        # 盈利后继续交易, 但下一单风险降至70%
        cooldowns[sym] = 0  # 清除冷却
    else:
        entry["losses"] = entry.get("losses", 0) + 1
        losses = entry["losses"]
        # 24.2: 连续亏损冷却
        if losses >= 3:
            cooldowns[sym] = time.time() + 86400  # 当天禁止
        elif losses >= 2:
            cooldowns[sym] = time.time() + 3600   # 冷却1小时
        elif losses >= 1:
            cooldowns[sym] = time.time() + 900    # 冷却15分钟

    _save(s)


# ─── 模式冷却 ─────────────────────────────────────

def mode_allows_trade(trade_type: TradeType) -> tuple[bool, str]:
    """返回 (允许, 原因)"""
    s = _load()
    cooldowns = s.get("mode_cooldowns", {})
    until = cooldowns.get(trade_type.value, 0)
    if time.time() < until:
        remaining = int(until - time.time())
        return False, f"模式{trade_type.value}冷却中, 剩余{remaining//60}分"
    return True, "ok"


def mode_performance(trade_type: TradeType) -> dict:
    """模式表现数据 (25章)"""
    s = _load()
    return s.get("mode", {}).get(trade_type.value, {})


def mode_risk_multiplier(trade_type: TradeType) -> float:
    """模式表现系数 (19.4)"""
    s = _load()
    m = s.get("mode", {}).get(trade_type.value, {})
    win_rate = m.get("last20_win_rate")
    avg_r = m.get("last20_avg_r")

    if win_rate is None or avg_r is None:
        return 1.0
    if win_rate < 0.40 or avg_r < 0:
        return 0.5
    if win_rate > 0.55 and avg_r > 0.3:
        return 1.2
    return 1.0


def record_mode_result(trade_type: TradeType, won: bool, r_val: float):
    """记录模式结果, 维护最近20笔滚动窗口 (25章)。"""
    s = _load()
    m = s.setdefault("mode", {}).setdefault(trade_type.value, {
        "results": [], "losses": 0, "cooldown_until": 0
    })
    mode_cooldowns = s.setdefault("mode_cooldowns", {})

    # 滚动窗口
    results = m.setdefault("results", [])
    results.append({"won": won, "r": r_val})
    if len(results) > 20:
        results.pop(0)

    # 统计
    if len(results) >= 5:
        wins = sum(1 for r in results if r["won"])
        m["last20_win_rate"] = round(wins / len(results), 2)
        m["last20_avg_r"] = round(sum(r["r"] for r in results) / len(results), 4)
    else:
        m["last20_win_rate"] = None
        m["last20_avg_r"] = None

    # 模式连续亏损
    if not won:
        m["losses"] = m.get("losses", 0) + 1
    else:
        m["losses"] = 0

    # 24.3: 模式冷却
    mode_losses = m["losses"]
    if trade_type == TradeType.MOMENTUM_SCALP and mode_losses >= 3:
        mode_cooldowns[trade_type.value] = time.time() + 3600  # 暂停30-60分钟
    elif trade_type == TradeType.FAILED_BREAKOUT and mode_losses >= 2:
        mode_cooldowns[trade_type.value] = time.time() + 1800  # 暂停30分钟
    elif trade_type in (TradeType.PULLBACK_STANDARD, TradeType.BREAKOUT_RETEST) and mode_losses >= 2:
        # 提高入场分数线5分 (由调用者处理)
        pass

    _save(s)


# ─── 每日模式配额 (26章) ──────────────────────────

def mode_quota_allows(trade_type: TradeType, all_open_trades: list) -> tuple[bool, str]:
    """每日模式配额检查。"""
    s = _load()
    a = s["account"]
    total_today = max(a["trades_today"], 1)

    # 计算今天各类已开仓数
    type_counts = {}
    for t in all_open_trades:
        tt = t.get("trade_type", "unknown")
        type_counts[tt] = type_counts.get(tt, 0) + 1

    # 动量快单 <= 当日交易次数60%
    if trade_type in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND):
        momentum_count = type_counts.get(TradeType.MOMENTUM_SCALP.value, 0) + type_counts.get(TradeType.MOMENTUM_SECOND.value, 0)
        if momentum_count / total_today > 0.6:
            return False, f"动量快单占比{type_counts.get(TradeType.MOMENTUM_SCALP.value, 0)}/天{total_today}已超过60%"

    # 同一方向连续动量快单不得超过3次
    if trade_type == TradeType.MOMENTUM_SCALP:
        consec_momentum = 0
        for t in reversed(all_open_trades[-10:]):
            if t.get("trade_type") in (TradeType.MOMENTUM_SCALP.value, TradeType.MOMENTUM_SECOND.value):
                consec_momentum += 1
            else:
                break
        if consec_momentum >= 3:
            return False, f"连续{consec_momentum}次动量快单, 已超3次限制"

    return True, "ok"

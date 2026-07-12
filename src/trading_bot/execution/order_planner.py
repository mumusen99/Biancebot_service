"""
交易计划生成器 (v4)
===================
27章: 开仓前最终检查
28章: 禁止行为清单

将策略层的结果 (routing, sl_tp, filters, risk)
整合为一个完整的 TradePlan, 通过 pre_trade_check() 验证后
再提交给交易所执行。
"""
import dataclasses, logging, time
from typing import Optional
from trading_bot.strategy.trade_router import Direction, TradeType
from trading_bot.strategy.sl_tp import calc_sl_tp, estimated_cost_pct, net_profit_ok
from trading_bot.risk.cooldown import (
    account_allows_trade, account_risk_multiplier,
    symbol_allows_trade, symbol_risk_multiplier,
    mode_allows_trade, mode_risk_multiplier,
    mode_quota_allows,
)
from trading_bot.strategy.filters import position_filter, btc_dominance_filter, overheat_filter

logger = logging.getLogger("v3")


@dataclasses.dataclass
class TradePlan:
    symbol: str
    direction: Direction
    trade_type: TradeType
    entry: float
    sl_hard: float
    sl_soft: float
    tp1: float
    tp2: float
    tp3: Optional[float]
    r_value: float
    rr_to_tp1: float
    risk_pct: float       # 风险百分比 (如0.8 = 0.8%)
    risk_usdt: float      # 最大亏损金额
    position_value: float # 仓位价值 (entry * qty)
    qty: float
    max_slippage_price: float
    use_market_order: bool
    time_stop_sec: int
    partial_take_profit: list  # [(price, fraction)]
    reason: str
    warnings: list
    net_cost_pct: float

    def to_state_entry(self) -> dict:
        return {
            "side": self.direction,
            "entry": self.entry,
            "sl_hard": self.sl_hard,
            "sl_soft": self.sl_soft,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "r": self.r_value,
            "rr": self.rr_to_tp1,
            "qty": self.qty,
            "trade_type": self.trade_type.value,
            "opened_at": time.time(),
            "entry_bars": 0,
            "hit_tp1": False,
            "reason": self.reason,
            "time_stopped": False,
            "last_candle_minute": int(time.time() // 60),
        }


# ─── 仓位计算 ────────────────────────────────────

def calc_position_value(
    balance: float,
    risk_pct: float,
    entry: float,
    sl_hard: float,
    max_leverage: int = 5,
) -> tuple[float, float]:
    """
    18章: 仓位由最大亏损反推。
    返回 (position_value, usdt_risk)
    """
    max_loss = balance * (risk_pct / 100)
    sl_pct = abs(entry - sl_hard) / entry
    if sl_pct <= 0:
        sl_pct = 0.01

    position_value = max_loss / sl_pct
    position_value = min(position_value, balance * max_leverage)
    return round(position_value, 2), round(max_loss, 4)


# ─── 动态风险计算 ────────────────────────────────

def calc_dynamic_risk(
    sym: str,
    trade_type: TradeType,
    balance: float,
) -> tuple[bool, float, str]:
    """
    19章: 动态风险系数。
    返回 (允许, 风险系数%, 否决原因)
    """
    # 账户级
    ok, reason = account_allows_trade()
    if not ok:
        return False, 0, reason

    # 模式级
    ok, reason = mode_allows_trade(trade_type)
    if not ok:
        return False, 0, reason

    # 单币种
    ok, reason = symbol_allows_trade(sym)
    if not ok:
        return False, 0, reason

    # ── 基础风险 (19.1) ──
    base_risk_map = {
        TradeType.PULLBACK_STANDARD: 1.0,
        TradeType.BREAKOUT_RETEST: 1.2,
        TradeType.MOMENTUM_SCALP: 0.4,
        TradeType.MOMENTUM_SECOND: 0.4,
        TradeType.FAILED_BREAKOUT: 0.8,
    }
    base_risk = base_risk_map.get(trade_type, 0.5)

    # 市场状态系数 (19.2)
    # (由调用者传入, 这里用默认)
    regime_factor = 1.0

    # 连亏惩罚 (19.3)
    loss_factor = account_risk_multiplier()

    # 单币惩罚
    sym_factor = symbol_risk_multiplier(sym)

    # 模式表现 (19.4)
    mode_factor = mode_risk_multiplier(trade_type)

    # 冷却后盈利 (24.2)
    wl_multiplier = 1.0

    actual_risk = base_risk * regime_factor * loss_factor * sym_factor * mode_factor * wl_multiplier

    # 类型上限
    max_risk_map = {
        TradeType.PULLBACK_STANDARD: 1.2,
        TradeType.BREAKOUT_RETEST: 1.5,
        TradeType.MOMENTUM_SCALP: 0.8,
        TradeType.MOMENTUM_SECOND: 0.5,
        TradeType.FAILED_BREAKOUT: 1.0,
    }
    actual_risk = min(actual_risk, max_risk_map.get(trade_type, 1.0))

    # 连亏>=3的已经上面拦了
    if loss_factor == 0:
        return False, 0, "连续亏损3单, 暂停"

    if actual_risk < 0.1:
        return False, 0, f"动态风险{actual_risk:.2f}%过低"

    return True, round(actual_risk, 2), "ok"


# ─── 预检查 ──────────────────────────────────────

def pre_trade_check(plan: TradePlan, balance: float) -> tuple[bool, list]:
    """
    27/28章: 开仓前最终检查。
    返回 (通过, 警告列表)
    """
    warnings = []

    # 28章禁止行为
    if plan.sl_hard is None or plan.sl_hard <= 0:
        warnings.append("禁止: 无止损")
    if plan.rr_to_tp1 < 0.5:
        warnings.append(f"禁止: RR={plan.rr_to_tp1:.1f} 太低")
    if plan.risk_pct <= 0:
        warnings.append("禁止: 风险为0")
    if plan.risk_usdt > balance * 0.05:
        warnings.append(f"禁止: 单笔风险{plan.risk_usdt:.2f}U > 5%余额")
    if plan.qty <= 0:
        warnings.append("禁止: 数量为0")

    # 净收益过滤 (先放低门槛, 确保能开单)
    if plan.net_cost_pct > 0:
        cost_mult = 2 if plan.trade_type in (TradeType.MOMENTUM_SCALP, TradeType.FAILED_BREAKOUT, TradeType.MOMENTUM_SECOND) else 3
        if plan.direction == Direction.LONG:
            gross_pct = (plan.tp1 - plan.entry) / plan.entry
        else:
            gross_pct = (plan.entry - plan.tp1) / plan.entry
        if gross_pct < plan.net_cost_pct * cost_mult:
            warnings.append(f"禁止: 净收益{gross_pct*100:.3f}% < 成本{plan.net_cost_pct*100:.3f}%×{cost_mult}")

    is_ok = len([w for w in warnings if "禁止" in w]) == 0
    return is_ok, warnings


# ─── 主构建函数 ──────────────────────────────────

def build_trade_plan(
    sym: str,
    trade_type: TradeType,
    direction: Direction,
    df_1m,
    df_5m,
    market: dict,
    balance: float,
    existing_positions: list,
    cost_spread: float = 0.0002,
) -> Optional[TradePlan]:
    """
    从路由器/过滤器/SLTP/风控结果生成完整 TradePlan。
    返回 None 如果任意核心检查失败。
    """
    # 1. 计算SL/TP
    sltp = calc_sl_tp(sym, df_1m, df_5m, trade_type, direction)
    entry = sltp["entry"]
    sl_h = sltp["sl_hard"]
    sl_s = sltp["sl_soft"]
    tp1 = sltp["tp1"]
    tp2 = sltp["tp2"]
    tp3 = sltp["tp3"]
    r_val = sltp["r"]
    time_stop = sltp["time_stop_sec"]
    partial = sltp.get("partial_take_profit", [])
    max_slip = sltp.get("max_slippage_price", entry)

    if r_val <= 0:
        return None

    # 2. 动态风险
    ok, risk_pct, reason = calc_dynamic_risk(sym, trade_type, balance)
    if not ok:
        return None

    # 3. 仓位计算
    position_value, risk_usdt = calc_position_value(balance, risk_pct, entry, sl_h)
    if position_value <= 0:
        return None

    # 4. 估算成本和净收益过滤
    cost_pct = estimated_cost_pct(cost_spread)
    net_ok = net_profit_ok(tp1, entry, direction, cost_pct, multiplier=2)

    # 5. 检查配额
    ok, reason = mode_quota_allows(trade_type, existing_positions)
    if not ok:
        return None

    # 配额限制下从简计算 qty
    qty = position_value / entry if entry > 0 else 0
    # 将对齐精度步骤留给调用者执行（需要 _align_qty）

    plan = TradePlan(
        symbol=sym,
        direction=direction,
        trade_type=trade_type,
        entry=entry,
        sl_hard=sl_h,
        sl_soft=sl_s,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        r_value=r_val,
        rr_to_tp1=round(abs(tp1 - entry) / max(r_val, 0.001), 1),
        risk_pct=risk_pct,
        risk_usdt=risk_usdt,
        position_value=position_value,
        qty=qty,
        max_slippage_price=max_slip,
        use_market_order=(trade_type in (TradeType.MOMENTUM_SCALP, TradeType.MOMENTUM_SECOND, TradeType.FAILED_BREAKOUT)),
        time_stop_sec=time_stop,
        partial_take_profit=partial,
        reason=sltp.get("reason_note", ""),
        warnings=[],
        net_cost_pct=cost_pct,
    )

    # 6. 预检查
    ok, warns = pre_trade_check(plan, balance)
    plan.warnings = warns
    if not ok:
        return None

    return plan

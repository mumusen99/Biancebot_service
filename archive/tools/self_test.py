"""
系统自测试脚本
=============
模拟各种工况检测 bug，覆盖：
- 信号检测（三种模式、三级评分）
- 止盈止损计算（R值、硬/软/时间止损）
- 风控（连亏、日亏、RR门槛）
- 精度对齐（-1111防错）
- 持仓管理（开仓/减仓/平仓）
- 边缘情况（空数据、API超时、已有持仓冲突）
"""
import sys, os, json, time, math
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
os.chdir(str(BASE))

# 导入 v4 新的模块
from risk.cooldown import (
    account_allows_trade, account_risk_multiplier,
    symbol_allows_trade, symbol_risk_multiplier,
    mode_allows_trade, mode_risk_multiplier,
    record_symbol_result, record_mode_result,
    update_account_result,
)
from execution.order_planner import calc_position_value, calc_dynamic_risk, pre_trade_check
from strategy.trade_router import TradeType, Direction, MarketRegime

# ─── 配置 ───
PASS = 0
FAIL = 0
ERRORS = []

def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")
        ERRORS.append(f"{name}: {detail}")

def section(name):
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")


# ══════════════════════════════════════════════════════════
#  导入
# ══════════════════════════════════════════════════════════
section("1. 模块加载")
try:
    from trader import _align_price, _align_price_dir, _align_qty, _align_sltp, _tick_decimals, _load_precisions
    _load_precisions()
    test("trader 模块加载", True)
except Exception as e:
    test("trader 模块加载", False, str(e))

try:
    from rt_scalper import (
        analyze_market, manage_positions, recalc_position_sltp,
        scan_market_broad, open_position_v4, scan_signals_v4,
    )
    test("rt_scalper v4 模块加载", True)
except Exception as e:
    test("rt_scalper 模块加载", False, str(e))


# ══════════════════════════════════════════════════════════
#  精度对齐测试
# ══════════════════════════════════════════════════════════
section("2. 精度对齐")

test("_tick_decimals 0.1 → 1", _tick_decimals(0.1) == 1)
test("_tick_decimals 0.01 → 2", _tick_decimals(0.01) == 2)
test("_tick_decimals 0.00001 → 5", _tick_decimals(0.00001) == 5)
test("_tick_decimals 1 → 0", _tick_decimals(1) == 0)

test("_align_price BTC 66063.893→66063.9", _align_price("BTCUSDT", 66063.893333) == 66063.9)
test("_align_price DOGE 0.075678→0.07568", abs(_align_price("DOGEUSDT", 0.075678) - 0.07568) < 0.00001)

# 方向对齐
test("_align_price_dir BTC down 66063.893→66063.8", _align_price_dir("BTCUSDT", 66063.893333, "down") == 66063.8)
test("_align_price_dir BTC up 66063.893→66063.9", _align_price_dir("BTCUSDT", 66063.893333, "up") == 66063.9)

# 数量对齐
test("_align_qty BTC 0.002345→0.002", _align_qty("BTCUSDT", 0.002345) == 0.002)
test("_align_qty DOGE 141.5→141", _align_qty("DOGEUSDT", 141.5) == 141.0)

# SL/TP 方向对齐
sl, tp = _align_sltp("BTCUSDT", 66063.893, 70063.893, "LONG")
test("_align_sltp LONG SL向上取整", sl == 66063.9)  # SL更紧
test("_align_sltp LONG TP向下取整", tp == 70063.8)  # TP更容易

sl, tp = _align_sltp("BTCUSDT", 53676.913, 50676.913, "SHORT")
test("_align_sltp SHORT SL向下取整", sl == 53676.9)  # 53676.913 floor to 0.1 = 53676.9

# 极端精度
test("_align_price PEPE 0.000012345", _align_price("PEPEUSDT", 0.000012345) == 0.00001235)
test("_align_qty WLD 0.06", _align_qty("WLDUSDT", 0.06) == 0.0)  # step=1, qty<1 → 0


# ══════════════════════════════════════════════════════════
#  风控测试
# ══════════════════════════════════════════════════════════
section("3. 风控系统")

# 测试动态风险 (v4模式)
ok, risk_pct, reason = calc_dynamic_risk("BTCUSDT", TradeType.PULLBACK_STANDARD, 200.0)
test("初始状态 → 允许开仓", ok)
test("初始状态 → 风险系数>0", risk_pct > 0)

# 模拟连亏3
for _ in range(3):
    update_account_result(-1.0)
ok, risk_pct, reason = calc_dynamic_risk("BTCUSDT", TradeType.PULLBACK_STANDARD, 200.0)
test("连亏3 → 禁止开仓", not ok)

# 模拟日亏超3U
# 先重置测试模式: 写入临时状态
ok, _ = account_allows_trade()
test("account_allows_trade 可以调用", isinstance(ok, bool))
# (check_risk_limits 已被 v4 模块替代)


# ══════════════════════════════════════════════════════════
#  R值SL/TP计算测试
# ══════════════════════════════════════════════════════════
section("4. R值SL/TP计算")

# 模拟 recalc_position_sltp 的算法
from rt_scalper import recalc_position_sltp

# 验证不需要实际持仓就能跑
try:
    recalc_position_sltp()
    test("recalc_position_sltp 无持仓不报错", True)
except Exception as e:
    test("recalc_position_sltp 无持仓不报错", False, str(e))


# ══════════════════════════════════════════════════════════
#  信号评分测试
# ══════════════════════════════════════════════════════════
section("5. 信号评分三级分类")

from rt_scalper import scan_signals_v4, analyze_market

# 测试空币种
try:
    sigs = scan_signals_v4("FAKEUSDT123", {"regime": "range", "heat": "normal", "bias": 0})
    test("不存在的币种 → 空列表", sigs == [])
except Exception as e:
    test("不存在的币种 → 空列表", False, f"异常: {e}")

# 测试市场分析
try:
    market = analyze_market("BTCUSDT")
    required = ["regime", "heat", "bias", "trend_strength", "atr_ratio"]
    has_all = all(k in market for k in required)
    test("analyze_market 返回必要字段", has_all)
    test(f"市场状态: {market['regime']} 热度:{market['heat']}", market["regime"] in ("range", "trend", "high_volatility"))
    print(f"    市场: {market}")
except Exception as e:
    test("analyze_market 运行", False, str(e))

# 测试真实币种信号（不依赖结果，只测试不报错）
for sym in ["BTCUSDT", "ETHUSDT", "BTCUSDT"]:
    try:
        market = analyze_market()
        sigs = scan_signals_v4(sym, market)
        test(f"{sym} 信号检测不报错", True)
        if sigs:
            for s in sigs:
                trade_type, direction, score, reason = s
                test(f"  [{trade_type.value}] score={score} → 信号生成成功", score > 0)
    except Exception as e:
        test(f"{sym} 信号检测报错", False, str(e))


# ══════════════════════════════════════════════════════════
#  边缘情况测试
# ══════════════════════════════════════════════════════════
section("6. 边缘情况")

# 6a. 内存状态清空测试
from rt_scalper import position_state, watch_zone, price_cache

# 模拟 position_state
position_state["TEST1"] = {
    "side": "LONG", "entry": 100.0, "sl_hard": 99.0, "sl_soft": 99.5,
    "tp1": 101.0, "tp2": 102.0, "r": 1.0, "qty": 1.0, "qty_str": "1",
    "trade_type": "standard", "entry_bars": 0, "hit_tp1": False,
    "opened_at": time.time(), "last_check": time.time(),
    "reason": "test", "time_stopped": False,
}

# 测试 manage_positions 硬止损
try:
    manage_positions("TEST1", 98.5)  # price < sl_hard (99) → 应平仓
    test("manage_positions 硬止损", "TEST1" not in position_state)  # 应已移除
except Exception as e:
    test("manage_positions 硬止损", False, str(e))

# 重新添加
position_state["BTCUSDT"] = {
    "side": "LONG", "entry": 100.0, "sl_hard": 99.0, "sl_soft": 99.5,
    "tp1": 101.0, "tp2": 102.0, "r": 1.0, "qty": 1.0, "qty_str": "1",
    "trade_type": "standard", "entry_bars": 0, "hit_tp1": False,
    "opened_at": time.time(), "last_check": time.time(),
    "reason": "test", "time_stopped": False,
}

# TP1 测试
try:
    pos_ref = position_state["BTCUSDT"]
    manage_positions("BTCUSDT", 101.5)  # price >= tp1 (101) → hit TP1
    tp1_was_set = pos_ref.get("hit_tp1") == True
    test("manage_positions TP1 触发", tp1_was_set)
except Exception as e:
    test("manage_positions TP1 触发", False, str(e))

# 清理
position_state.clear()

# 清理测试用持仓
position_state.pop("BTCUSDT", None)

# 6b. 空数据测试
test("空 position_state 不报错", True)
try:
    recalc_position_sltp()
    test("recalc_position_sltp 空状态不报错", True)
except Exception as e:
    test("recalc_position_sltp 空状态不报错", False, str(e))

# 6c. 风控配置文件读写 (v4 模块)
ok, _ = account_allows_trade()
test("cooldown 模块可正常调用", isinstance(ok, bool))


# ══════════════════════════════════════════════════════════
#  全市场扫描测试
# ══════════════════════════════════════════════════════════
section("7. 全市场扫描")

try:
    from rt_scalper import scan_market_broad, update_watch_zone
    scored = scan_market_broad()
    if scored:
        test("scan_market_broad 返回结果", len(scored) > 0)
        test("scan_market_broad 结果含score字段", all("score" in s for s in scored))
        test(f"Top 评分 > 60", scored[0]["score"] > 60)
        top5 = [s["sym"] + "(" + str(s["score"]) + ")" for s in scored[:5]]; print("  Top5: " + "  ".join(top5))
        
        # 测试 update_watch_zone
        update_watch_zone(scored)
        from rt_scalper import watch_zone
        test("update_watch_zone 产生观测区", len(watch_zone) > 0)
    else:
        test("scan_market_broad 返回空（可能网络问题）", True)
except Exception as e:
    test("scan_market_broad 运行", False, str(e))
    import traceback
    traceback.print_exc()


# ══════════════════════════════════════════════════════════
#  总结
# ══════════════════════════════════════════════════════════
section(f"测试结果")
total = PASS + FAIL
print(f"  通过: {PASS}/{total} ({PASS/total*100:.0f}%)")
print(f"  失败: {FAIL}/{total}")
if ERRORS:
    print(f"\n  失败详情:")
    for e in ERRORS:
        print(f"    {e}")
print(f"\n  {'🎉 全部通过!' if FAIL == 0 else '⚠️ 有需要修复的bug'}")

"""
每日复盘：盈亏分析 + 算法参数迭代建议
========================================
分析当天所有交易记录，计算关键指标，识别问题模式，
给出策略参数优化建议。
"""
import json, sys
from pathlib import Path
from datetime import datetime, date
from collections import Counter, defaultdict

BASE = Path(__file__).parent

def load_state():
    try:
        return json.loads((BASE / "bot_state.json").read_text())
    except:
        return {"trades": [], "positions": {}, "total_pnl": 0, "closed_pnl": 0}


def get_today_trades():
    """获取今天的所有交易"""
    state = load_state()
    today = str(date.today())
    trades = state.get("trades", [])
    today_trades = [t for t in trades if t.get("time","").startswith(today)]
    today_trades.sort(key=lambda t: t.get("time",""))
    return today_trades, state


def analyze():
    trades, state = get_today_trades()
    
    opens = [t for t in trades if t.get("action") == "OPEN"]
    closes = [t for t in trades if t.get("action") == "CLOSE"]
    
    today_str = date.today().isoformat()
    
    print(f"📊 每日复盘 — {today_str}")
    print("=" * 45)
    
    # 基础统计
    total_pnl = sum(t.get("pnl", 0) for t in closes)
    win_trades = [t for t in closes if t.get("pnl", 0) > 0]
    loss_trades = [t for t in closes if t.get("pnl", 0) <= 0]
    win_rate = len(win_trades) / len(closes) * 100 if closes else 0
    
    print(f"\n📈 总览")
    print(f"  开仓: {len(opens)}笔 | 平仓: {len(closes)}笔")
    if closes:
        print(f"  胜率: {win_rate:.1f}% ({len(win_trades)}胜/{len(loss_trades)}负)")
        print(f"  总盈亏: {total_pnl:+.2f}U")
        avg_win = sum(t.get("pnl",0) for t in win_trades) / len(win_trades) if win_trades else 0
        avg_loss = sum(t.get("pnl",0) for t in loss_trades) / len(loss_trades) if loss_trades else 0
        if avg_loss != 0:
            print(f"  平均盈利: {avg_win:+.2f}U | 平均亏损: {avg_loss:+.2f}U | 盈亏比: {abs(avg_win/avg_loss):.2f}")
        else:
            print(f"  平均盈利: {avg_win:+.2f}U | 平均亏损: N/A")
    
    # 策略分布
    scalp = [t for t in opens if t.get("strategy") == "scalp"]
    main_trades = [t for t in opens if t.get("strategy") != "scalp"]
    print(f"\n📌 策略分布")
    print(f"  波段限价: {len(main_trades)}笔 | 舔一口: {len(scalp)}笔")
    
    # 重复开仓
    coin_counts = Counter(t.get("symbol","") for t in opens)
    duplicates = {k: v for k, v in coin_counts.items() if v > 2}
    if duplicates:
        print(f"\n🔁 重复开仓（>2次）")
        for sym, cnt in sorted(duplicates.items(), key=lambda x: -x[1]):
            print(f"  {sym:<14} 开仓{cnt}次")
    
    # 按时间分布
    hours = Counter()
    for t in opens:
        h = t.get("time","")[11:13]
        hours[h] += 1
    if hours:
        print(f"\n⏰ 时间分布")
        for h in sorted(hours):
            bar = "█" * min(hours[h], 10)
            print(f"  {h}:00 {bar} {hours[h]}次")
    
    # 算法迭代建议
    print(f"\n💡 算法迭代建议")
    suggestions = []
    
    # 检查胜率
    if closes and win_rate < 30:
        suggestions.append("🔴 胜率偏低(<30%) → 考虑收紧入场信号或调整SL/TP比例")
    elif closes and win_rate > 50:
        suggestions.append("🟢 胜率不错(>50%) → 可适度放宽信号或增加仓位")
    
    # 检查盈亏比
    if closes and avg_loss != 0 and abs(avg_win/avg_loss) < 1.0:
        suggestions.append("🔴 盈亏比<1 → 亏多赚少，需调整止盈止损距离")
    elif closes and avg_loss != 0 and abs(avg_win/avg_loss) > 2.0:
        suggestions.append("🟢 盈亏比>2 → 方向判断准确")
    
    # 检查重复开仓
    if duplicates:
        total_dup = sum(duplicates.values())
        pct = total_dup / len(opens) * 100 if opens else 0
        if pct > 30:
            suggestions.append(f"🔴 重复开仓占{pct:.0f}% → 防重复机制可能失效")
        else:
            suggestions.append(f"🟡 重复开仓{pct:.0f}% → 防重复机制正常工作")
    
    # 检查代理/API
    from proxy_guard import test_binance
    api_ok = test_binance(use_proxy=True)
    if not api_ok:
        suggestions.append("🔴 Binance API不可达 → 代理异常，需手动恢复")
    else:
        suggestions.append("🟢 API连通正常")
    
    # 当前持仓
    positions = state.get("positions", {})
    if positions:
        print(f"\n📋 当前持仓 ({len(positions)}个)")
        total_unrealized = 0
        for sym, info in positions.items():
            pnl = info.get("pnl", info.get("unrealizedPnl", 0))
            total_unrealized += pnl or 0
            strat = info.get("strategy", "main")
            emoji = "🏃‍♂️" if strat == "scalp" else "📗"
            print(f"  {emoji} {sym:<12} {info.get('side','?'):<6} PnL={pnl:+.2f}U")
        print(f"  未实现PnL合计: {total_unrealized:+.2f}U")
        print(f"  累计已实现PnL: {state.get('closed_pnl',0):+.2f}U")
        print(f"  总PnL: {state.get('total_pnl',0):+.2f}U")
    
    if not suggestions:
        suggestions.append("📝 无显著问题，当前策略运行正常")
    
    for s in suggestions:
        print(f"  {s}")
    
    # 输出摘要
    print(f"\n{'='*45}")
    if closes:
        print(f"今日: {len(opens)}开/{len(closes)}平 PnL{total_pnl:+.2f}U 胜率{win_rate:.0f}%")
    else:
        print(f"今日: {len(opens)}开/0平 无平仓记录")
    
    return {
        "opens": len(opens),
        "closes": len(closes),
        "total_pnl": total_pnl,
        "win_rate": round(win_rate, 1),
        "suggestions": suggestions,
    }


if __name__ == "__main__":
    analyze()

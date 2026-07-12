"""
舔一口快速执行（15分钟触发）
=========================
只做：代理检查 → 限价单成交检测 → 修复止盈止损 → 信号扫描
跳过：全市场筛选、KOL搜索、波段限价、复查
"""
import json, time, logging, sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [scalp] %(message)s")
logger = logging.getLogger("scalp_quick")

BASE = Path(__file__).parent
start = time.time()

def main():
    os.chdir(str(BASE))

    # 1. 代理检查
    from proxy_guard import ensure_connection
    if not ensure_connection():
        print("❌ Binance 不可达，跳过")
        return

    # 2. 舔一口
    import scalper
    scalper.run_scalper()

    # 2.5 统一仓位管理（同步+追踪止损+时间退出）
    from position_manager import run_full_cycle
    run_full_cycle()

    # 3. 最近平仓
    from auto_trader import get_recent_closes
    closes = get_recent_closes(hours=0.5)
    if closes:
        total = sum(c['pnl'] for c in closes)
        wins = sum(1 for c in closes if c['pnl'] > 0)
        losses = sum(1 for c in closes if c['pnl'] < 0)
        print(f"\n━━━ 最近平仓（API）━━━")
        for c in closes:
            tm = time.strftime('%H:%M', time.localtime(c['time']/1000))
            icon = '✅' if c['pnl'] > 0 else '🛑'
            pnl_s = f"{c['pnl']:+.4f}U" if abs(c['pnl']) >= 0.01 else f"{c['pnl']:+.6f}U"
            print(f"  {tm} {icon} {c['sym']:12s} {c['side']:6s}  {pnl_s}")
        print(f"  {'─'*40}")
        print(f"  合计 {len(closes)}笔  ✅{wins}胜 ❌{losses}负  总PnL {total:+.4f}U")

    dur = time.time() - start
    print(f"\n⏱ 舔一口耗时: {dur:.0f}s")
    print(f"\n✅ 本轮舔一口完成")

if __name__ == "__main__":
    import os
    main()

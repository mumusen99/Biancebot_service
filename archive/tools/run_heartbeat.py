"""
一键心跳：清理 → 波段 → 超短线 → 复查 → 报告
单进程执行，复用连接，减少重复API调用
"""
import json, time, logging, sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("heartbeat")

BASE = Path(__file__).parent


def run_step(name: str, module: str):
    """执行模块并捕获输出"""
    logger.info(f"━━━ [{name}] ━━━")
    try:
        import importlib, io
        old_stdout = sys.stdout
        sys.stdout = buf = io.StringIO()

        mod = importlib.import_module(module)
        if hasattr(mod, "run_full_cycle"):
            mod.run_full_cycle()
        elif hasattr(mod, "main"):
            mod.main()
        elif hasattr(mod, "run_cycle"):
            mod.run_cycle()
        elif hasattr(mod, "run_scalper"):
            mod.run_scalper()
        elif hasattr(mod, "report"):
            mod.report()

        sys.stdout = old_stdout
        output = buf.getvalue()
        if output.strip():
            print(output.rstrip())
        return output
    except Exception as e:
        sys.stdout = old_stdout
        logger.error(f"❌ {name} 失败: {e}")
        import traceback
        print(traceback.format_exc())
        return ""


def main():
    # 确保在工作目录
    import os
    os.chdir(str(BASE))

    # 0. 代理/API 连通性检查 + 自动恢复
    from proxy_guard import ensure_connection
    if not ensure_connection():
        print("❌ Binance API 不可达，跳过本轮心跳")
        return
    print()

    # 1. 清理重复委托（快，~3s）
    run_step("清理重复", "check_clean_orders")

    # 2. 波段限价单（慢，含KOL搜索 ~30-60s）
    run_step("波段限价", "auto_trader")

    # 3. 复查所有挂单+条件委托（~10-15s）
    # ⚠️ 舔一口超短线已分离到 15min cron（run_scalper_quick.py）
    run_step("复查", "review_orders")

    # 3.5 统一仓位管理：同步交易所→打标→止盈止损→追踪→时间退出
    run_step("仓位管理", "position_manager")

    # 环境摘要
    from market_regime import get_btc_environment
    env = get_btc_environment()

    # 4. 最近平仓（从交易所 API 查）
    from auto_trader import get_recent_closes
    closes = get_recent_closes(hours=2)
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

    # 5. 清通知队列
    notif_file = BASE / "notifications.json"
    if notif_file.exists():
        try:
            notif = json.loads(notif_file.read_text())
            if notif:
                print(f"\n📝 通知队列: {len(notif)}条")
        except:
            pass
        notif_file.write_text("[]")

    print("\n✅ 本轮心跳完成")


if __name__ == "__main__":
    main()

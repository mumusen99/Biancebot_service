"""
v4 策略回测系统
=============
覆盖多种行情环境，模拟完整的五种交易类型策略。

运行: python backtest_v4.py
输出: backtest_report.txt + backtest_trades.csv
"""
import sys, os, json, time, math
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
os.chdir(str(BASE))

import pandas as pd
from data_fetcher import fetch_klines
from scalper import compute_indicators
from strategy.trade_router import Direction, TradeType, classify_direction, route_trade_type, SymbolData
from strategy.trade_router import ema as calc_ema, slope as calc_slope
from strategy.sl_tp import calc_sl_tp, estimated_cost_pct
from strategy.filters import position_filter, btc_dominance_filter, overheat_filter
from risk.cooldown import (
    update_account_result, account_allows_trade,
    symbol_allows_trade, symbol_risk_multiplier,
    mode_allows_trade, mode_risk_multiplier,
    record_symbol_result, record_mode_result,
)

os.environ['TZ'] = 'Asia/Shanghai'

# ─── 配置 ───────────────────────────────────────────

INITIAL_BALANCE = 200
LEVERAGE = 5
MAX_POSITIONS = 10

# 不同行情时段（5m数据，每段约4天 = 1000+ bars）
PERIODS = {
    "trend_up":   {"coins": ["BTCUSDT", "SOLUSDT", "ETHUSDT"],   "label": "上升趋势"},
    "range":      {"coins": ["XRPUSDT", "ADAUSDT", "BNBUSDT"],   "label": "震荡行情"},
    "high_vol":   {"coins": ["DOGEUSDT", "PEPEUSDT", "WIFUSDT"], "label": "高波动行情"},
    "downtrend":  {"coins": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],   "label": "下跌趋势"},
}

# We'll let the strategy use whatever data is available
SCAN_INTERVAL_BARS = 12    # 每12根5m K线扫一次信号 (~1小时)
HEAVY_INTERVAL_BARS = 2    # 每2根5m K线管理一次持仓 (~10分钟)

# ─── 回测引擎 ──────────────────────────────────────

class BacktestEngine:
    def __init__(self, balance=INITIAL_BALANCE):
        self.balance = balance
        self.peak_balance = balance
        self.positions = {}
        self.trades = []
        self.equity_curve = []
        self.watch_zone = {}
        self.price_cache = {}
        self.bar_count = 0
        self.market_state = {"regime": "range", "bias": 0, "heat": "normal"}
        
        # Performance tracking
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.max_drawdown = 0.0
        
    def open_position(self, sym, trade_type, direction, entry, sl_hard, sl_soft, tp1, tp2, r_val, score, reason, bar_time):
        if sym in self.positions:
            return False
        
        # Size: 1% risk of balance, capped
        risk_pct = 1.0
        max_loss = self.balance * (risk_pct / 100)
        sl_pct = abs(entry - sl_hard) / entry if entry > 0 else 0.01
        if sl_pct <= 0:
            sl_pct = 0.01
        position_value = min(max_loss / sl_pct, self.balance * LEVERAGE)
        qty = position_value / entry if entry > 0 else 0
        if qty <= 0:
            return False
        margin_used = position_value / LEVERAGE
        if margin_used > self.balance * 0.3:
            qty = (self.balance * 0.3 * LEVERAGE) / entry
            position_value = qty * entry
        
        self.positions[sym] = {
            "side": direction.value,
            "entry": entry,
            "sl_hard": sl_hard,
            "sl_soft": sl_soft,
            "tp1": tp1,
            "tp2": tp2,
            "r": r_val,
            "qty": qty,
            "trade_type": trade_type.value,
            "score": score,
            "reason": reason,
            "opened_at": self.bar_count,
            "entry_bars": 0,
            "hit_tp1": False,
            "time_stopped": False,
            "last_bar": 0,
        }
        return True
    
    def close_position(self, sym, price, reason):
        pos = self.positions.pop(sym, None)
        if not pos:
            return 0
        side = pos["side"]
        entry = pos["entry"]
        qty = pos["qty"]
        if side == "LONG":
            pnl = (price - entry) * qty
        else:
            pnl = (entry - price) * qty
        pnl_after_fee = pnl * 0.999  # ~0.1% fee
        
        self.trades.append({
            "symbol": sym,
            "side": side,
            "trade_type": pos["trade_type"],
            "entry": entry,
            "exit": price,
            "qty": qty,
            "pnl": round(pnl_after_fee, 4),
            "reason": reason,
            "bars_held": pos.get("entry_bars", 0),
            "score": pos.get("score", 0),
            "bar": self.bar_count,
        })
        
        self.total_pnl += pnl_after_fee
        self.balance += pnl_after_fee
        self.peak_balance = max(self.peak_balance, self.balance)
        dd = (self.peak_balance - self.balance) / self.peak_balance * 100
        self.max_drawdown = max(self.max_drawdown, dd)
        
        if pnl_after_fee > 0:
            self.wins += 1
        else:
            self.losses += 1
        
        # Update risk state
        update_account_result(pnl_after_fee)
        record_symbol_result(sym, pnl_after_fee > 0)
        try:
            tt_enum = TradeType(pos["trade_type"])
        except ValueError:
            tt_enum = TradeType.PULLBACK_STANDARD
        record_mode_result(tt_enum, pnl_after_fee > 0, pos.get("r", 0))
        
        return pnl_after_fee
    
    def manage_positions(self, sym, price):
        """每分钟检查持仓"""
        pos = self.positions.get(sym)
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
            hit_sl = price <= sl_h
            hit_tp1_now = not hit_tp1 and tp1 > 0 and price >= tp1
            hit_tp2 = tp2 > 0 and price >= tp2
        else:
            current_r = (entry - price) / r_val if r_val > 0 else 0
            hit_sl = price >= sl_h
            hit_tp1_now = not hit_tp1 and tp1 > 0 and price <= tp1
            hit_tp2 = tp2 > 0 and price <= tp2
        
        # Hard stop
        if hit_sl:
            self.close_position(sym, price, f"硬止损 R{current_r:.1f}")
            return
        
        # Soft stop
        if (side == "LONG" and price <= sl_s) or (side == "SHORT" and price >= sl_s):
            if current_r < 0:
                self.close_position(sym, price, f"软止损 R{current_r:.1f}")
                return
        
        # TP2 full close
        if hit_tp2:
            self.close_position(sym, price, f"TP2 R{current_r:.1f}")
            return
        
        # Momentum 2-min rule
        if trade_type in ("momentum_scalp", "momentum_second_entry") and pos.get("entry_bars", 0) >= 2 and current_r < 0.4:
            self.close_position(sym, price, "动量2分钟未达TP1")
            return
        
        # TP1 partial close (simplified: close 40% at TP1, rest trails)
        if hit_tp1_now:
            pct = 0.4
            pos["hit_tp1"] = True
            # Simulate partial close: adjust position size
            pos["qty"] = pos["qty"] * (1 - pct)
            # Move SL to entry
            if side == "LONG":
                pos["sl_hard"] = max(pos["sl_hard"], entry * 0.998)
            else:
                pos["sl_hard"] = min(pos["sl_hard"], entry * 1.002)
        
        # Time stop
        bars = pos.get("entry_bars", 0)
        timeout_map = {"momentum_scalp": 3, "momentum_second_entry": 3,
                       "failed_breakout_reversal": 5, "pullback_standard": 5,
                       "breakout_retest": 5, "standard": 10}
        timeout = timeout_map.get(trade_type, 5)
        if bars >= timeout and current_r < -0.3 and not pos.get("time_stopped", False):
            pos["time_stopped"] = True
            self.close_position(sym, price, f"时间止损 {bars}bars")
            return
        
        # Update entry_bars
        pos["entry_bars"] = pos.get("entry_bars", 0) + 1
    
    def calculate_metrics(self):
        """计算绩效指标"""
        n = len(self.trades)
        if n == 0:
            return {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0, "final_balance": INITIAL_BALANCE, "max_drawdown_pct": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 0.0, "sharpe": 0.0, "avg_bars_held": 0.0, "win_rate": 0}
        
        win_rate = self.wins / n * 100 if n > 0 else 0
        avg_win = sum(t["pnl"] for t in self.trades if t["pnl"] > 0) / max(self.wins, 1)
        avg_loss = abs(sum(t["pnl"] for t in self.trades if t["pnl"] < 0)) / max(self.losses, 1)
        
        # Sharpe-like (simplified)
        returns = [t["pnl"] / INITIAL_BALANCE for t in self.trades]
        avg_ret = sum(returns) / len(returns) if returns else 0
        std_ret = (sum((r - avg_ret)**2 for r in returns) / len(returns))**0.5 if returns else 1
        sharpe = avg_ret / max(std_ret, 0.0001) * (252 * 288)**0.5  # annualized (~288 5m bars/day)
        
        return {
            "trades": n,
            "wins": self.wins if hasattr(self, 'wins') else 0,
            "losses": self.losses if hasattr(self, 'losses') else 0,
            "win_rate": round(win_rate, 1) if n > 0 else 0.0,
            "total_pnl": round(self.total_pnl, 2) if hasattr(self, 'total_pnl') else 0.0,
            "final_balance": round(self.balance, 2) if hasattr(self, 'balance') else INITIAL_BALANCE,
            "max_drawdown_pct": round(self.max_drawdown, 2) if hasattr(self, 'max_drawdown') else 0.0,
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "profit_factor": round(avg_win * self.wins / max(avg_loss * self.losses, 0.001), 2),
            "sharpe": round(sharpe, 2),
            "avg_bars_held": round(sum(t.get("bars_held", 0) for t in self.trades) / max(n, 1), 1),
        }
    
    def print_summary(self, label=""):
        m = self.calculate_metrics()
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
        print(f"  交易次数: {m['trades']}")
        print(f"  胜率:     {m['win_rate']}% ({m['wins']}盈/{m['losses']}亏)")
        print(f"  总盈亏:   {m['total_pnl']:+.2f}U")
        print(f"  余额:     {INITIAL_BALANCE} → {m['final_balance']}U")
        print(f"  最大回撤: {m['max_drawdown_pct']}%")
        print(f"  平均盈利: {m['avg_win']:.4f}U")
        print(f"  平均亏损: {m['avg_loss']:.4f}U")
        print(f"  盈亏比:   {m['profit_factor']}")
        print(f"  Sharpe:   {m['sharpe']}")
        print(f"  平均持仓: {m['avg_bars_held']} 根K线")
        return m


# ─── 回测运行 ──────────────────────────────────────

def run_backtest(symbols: list, label: str, progress_callback=None) -> dict:
    """对一组币种运行回测"""
    engine = BacktestEngine()
    total_bars = 0
    
    for sym in symbols:
        if progress_callback:
            progress_callback(f"  {sym}...")
        
        # Fetch recent 5m data (1000 bars ≈ 3.5 days)
        df_5m_all = fetch_klines(None, sym, "5m", 1000)
        if df_5m_all is None or df_5m_all.empty or len(df_5m_all) < 200:
            continue
        
        # Also fetch 1m data for direction classification
        df_1m_all = fetch_klines(None, sym, "1m", 1000)
        if df_1m_all is None or df_1m_all.empty:
            continue
        
        # Pre-compute indicators
        df_5m_all = compute_indicators(df_5m_all)
        df_1m_all = compute_indicators(df_1m_all)
        
        total = len(df_5m_all)
        engine.bar_count = 0
        
        # Also need BTC data for filters
        df_btc_5m = fetch_klines(None, "BTCUSDT", "5m", 1000)
        df_btc_5m = compute_indicators(df_btc_5m) if df_btc_5m is not None and not df_btc_5m.empty else df_5m_all
        
        for i in range(60, total):  # Start at 60 to have enough lookback
            engine.bar_count = engine.bar_count + 1
            bar = df_5m_all.iloc[i]
            price = float(bar["close"])
            ts = bar["timestamp"]
            
            # Update price cache
            engine.price_cache[sym] = price
            
            # Skip if BTC data doesn't match
            if len(df_btc_5m) < 60:
                continue
            
            # ── Manage existing positions ──
            for p_sym in list(engine.positions.keys()):
                p_price = engine.price_cache.get(p_sym, price)
                engine.manage_positions(p_sym, p_price)
            
            # ── Heavy tick: signal scan ──
            if engine.bar_count % HEAVY_INTERVAL_BARS == 0 and len(engine.positions) < MAX_POSITIONS:
                try:
                    # Build data for routing
                    lookback_5m = df_5m_all.iloc[max(0, i-60):i+1]
                    lookback_1m_start = max(0, (i * 5) - 60)  # approximate 1m offset
                    lookback_1m = df_1m_all.iloc[-min(60, len(df_1m_all)):]
                    
                    if len(lookback_5m) < 30 or len(lookback_1m) < 25:
                        continue
                    
                    direction = classify_direction(lookback_1m, lookback_5m)
                    if direction is None:
                        continue
                    
                    last5 = lookback_5m.iloc[-1]
                    closes_5m = [float(r["close"]) for _, r in lookback_5m.iterrows()]
                    closes_1m = [float(r["close"]) for _, r in lookback_1m.iterrows()]
                    
                    d = SymbolData(
                        symbol=sym,
                        closes_1m=closes_1m,
                        df_1m=lookback_1m,
                        closes_5m=closes_5m,
                        df_5m=lookback_5m,
                        price=price,
                        ema20_1m=calc_ema(closes_1m, 20),
                        ema20_5m=calc_ema(closes_5m, 20),
                        ema9_5=float(last5.get("ema9", price)),
                        ema21_5=float(last5.get("ema21", price)),
                        atr1=float(lookback_1m.iloc[-1].get("atr", price*0.002)),
                        atr5=float(last5.get("atr", price*0.005)),
                        rsi5=float(last5.get("rsi", 50)),
                        vol_ratio=float(last5.get("volume", 0)) / max(float(last5.get("vol_avg", 1)), 0.01),
                        swing_low=float(last5.get("swing_low", price*0.99)),
                        swing_high=float(last5.get("swing_high", price*1.01)),
                        bias=engine.market_state.get("bias", 0),
                        regime=engine.market_state.get("regime", "range"),
                        heat=engine.market_state.get("heat", "normal"),
                    )
                    
                    trade_type, dir, reason = route_trade_type(d)
                    if trade_type == TradeType.NO_TRADE:
                        continue
                    
                    # Score
                    score = 60 + (15 if (d.ema9_5 > d.ema21_5 and dir == Direction.LONG) or (d.ema9_5 < d.ema21_5 and dir == Direction.SHORT) else 0)
                    score += 8 if 35 <= d.rsi5 <= 60 else 0
                    score += 5 if d.vol_ratio > 1.3 else 0
                    if score < 60:
                        continue
                    
                    # Calculate SL/TP
                    sltp = calc_sl_tp(sym, lookback_1m, lookback_5m, trade_type, dir)
                    entry_price = sltp["entry"]
                    
                    # Open position
                    engine.open_position(
                        sym, trade_type, dir,
                        entry_price,
                        sltp["sl_hard"], sltp["sl_soft"],
                        sltp["tp1"], sltp["tp2"],
                        sltp["r"], score, reason,
                        engine.bar_count,
                    )
                    
                except Exception:
                    pass
    
    return engine.calculate_metrics()


def print_all_results(all_results):
    """打印汇总"""
    print(f"\n{'='*70}")
    print(f"  v4 策略回测汇总")
    print(f"{'='*70}")
    
    combined = {
        "trades": 0, "wins": 0, "losses": 0,
        "total_pnl": 0, "max_dd": 0,
    }
    
    for label, metrics in all_results.items():
        combined["trades"] += metrics["trades"]
        combined["wins"] += metrics["wins"]
        combined["losses"] += metrics["losses"]
        combined["total_pnl"] += metrics["total_pnl"]
        combined["max_dd"] = max(combined["max_dd"], metrics["max_drawdown_pct"])
    
    if combined["trades"] > 0:
        wr = combined["wins"] / combined["trades"] * 100
    else:
        wr = 0
    
    print(f"\n  总交易:     {combined['trades']}")
    print(f"  总胜率:     {wr:.1f}% ({combined['wins']}盈/{combined['losses']}亏)")
    print(f"  总盈亏:     {combined['total_pnl']:+.2f}U")
    print(f"  最大回撤:   {combined['max_dd']:.2f}%")
    print(f"  收益率:     {combined['total_pnl']/INITIAL_BALANCE*100:+.2f}%")


# ─── 主入口 ────────────────────────────────────────

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.ERROR)
    
    print("=" * 70)
    print("  v4 策略回测")
    print(f"  初始余额: {INITIAL_BALANCE}U  杠杆: {LEVERAGE}x")
    print("=" * 70)
    
    # Use real market data - fetch recent data for top coins
    # This represents a mix of market conditions
    test_coins = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
        "ADAUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT",
    ]
    
    all_results = {}
    
    print(f"\n正在回测 {len(test_coins)} 个币种...\n")
    
    for i, sym in enumerate(test_coins):
        print(f"[{i+1}/{len(test_coins)}] {sym}", end=" ", flush=True)
        try:
            result = run_backtest([sym], sym)
            all_results[sym] = result
            print(f"→ {result['trades']}笔  PnL={result['total_pnl']:+.2f}U  胜率{result['win_rate']}%")
        except Exception as e:
            print(f"→ 失败: {e}")
    
    # Print summary
    print_all_results(all_results)
    
    # Detailed per-coin
    print(f"\n{'='*70}")
    print(f"  各币种详情")
    print(f"{'='*70}")
    print(f"{'币种':<12s} {'交易':>5s} {'胜率':>6s} {'盈亏':>8s} {'回撤':>6s} {'Sharpe':>7s}")
    print("-" * 50)
    for sym, m in all_results.items():
        if m["trades"] > 0:
            print(f"{sym:<12s} {m['trades']:>5d} {m['win_rate']:>5.1f}% {m['total_pnl']:>+7.2f}U {m['max_drawdown_pct']:>5.1f}% {m['sharpe']:>7.2f}")
    
    # Save report
    with open("backtest_report.txt", "w") as f:
        f.write(f"v4 策略回测报告\n")
        f.write(f"时间: {datetime.now()}\n")
        f.write(f"初始余额: {INITIAL_BALANCE}U\n\n")
        for sym, m in all_results.items():
            f.write(f"{sym}: {m['trades']}笔, 胜率{m['win_rate']}%, PnL={m['total_pnl']:+.2f}U, 回撤{m['max_drawdown_pct']}%\n")
    
    print(f"\n✅ 报告已保存: backtest_report.txt")

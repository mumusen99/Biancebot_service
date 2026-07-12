"""
策略回测引擎 v1
==============
对历史K线数据运行当前策略，评估表现。
支持波段（1h）和超短线（5m）两种策略的回测。
"""
import json, time, logging, sys
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import pandas as pd
import numpy as np

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

from data_fetcher import fetch_klines
from scalper import compute_indicators
from config import STOP_LOSS_PERCENT, TAKE_PROFIT_PERCENT, DEFAULT_LEVERAGE


def backtest_band(symbol: str, days: int = 7):
    """回测波段策略（1h K线，LIMIT入场，固定止盈止损）"""
    df = fetch_klines(None, symbol, "1h", days * 24 + 100)
    if df is None or df.empty:
        return None
    
    df = compute_indicators(df)
    sl_pct = STOP_LOSS_PERCENT / DEFAULT_LEVERAGE / 100  # 6.7% → 0.067
    tp_pct = TAKE_PROFIT_PERCENT / DEFAULT_LEVERAGE / 100  # 13.3% → 0.133
    
    trades = []
    position = None  # {'side', 'entry', 'exit', 'pnl'}
    
    for i in range(30, len(df)-1):
        row = df.iloc[i]
        next_row = df.iloc[i+1]
        
        close = float(row['close'])
        ema9 = float(row.get('ema9', close))
        ema21 = float(row.get('ema21', close))
        rsi = float(row.get('rsi', 50))
        open_next = float(next_row['open'])
        high_next = float(next_row['high'])
        low_next = float(next_row['low'])
        
        trend_up = ema9 > ema21
        near_ema9 = ema9 * 0.995 <= close <= ema9 * 1.005
        
        # 入场信号
        if position is None:
            long_signal = trend_up and near_ema9 and 40 <= rsi <= 58
            short_signal = not trend_up and near_ema9 and 42 <= rsi <= 60
            
            if long_signal:
                entry = close
                sl = entry * (1 - sl_pct)
                tp = entry * (1 + tp_pct)
                position = {'side': 'LONG', 'entry': entry, 'sl': sl, 'tp': tp}
            elif short_signal:
                entry = close
                sl = entry * (1 + sl_pct)
                tp = entry * (1 - tp_pct)
                position = {'side': 'SHORT', 'entry': entry, 'sl': sl, 'tp': tp}
        
        # 检查退出
        if position:
            side = position['side']
            sl = position['sl']
            tp = position['tp']
            
            hit_sl = (side == 'LONG' and low_next <= sl) or (side == 'SHORT' and high_next >= sl)
            hit_tp = (side == 'LONG' and high_next >= tp) or (side == 'SHORT' and low_next <= tp)
            
            if hit_sl:
                pnl_pct = (sl - position['entry']) / position['entry'] if side == 'LONG' else (position['entry'] - sl) / position['entry']
                pnl = pnl_pct * DEFAULT_LEVERAGE * 20  # 20U保证金
                trades.append({'entry_time': row.name, 'side': side, 'entry': position['entry'], 
                               'exit': sl, 'pnl': round(pnl, 4), 'reason': 'SL'})
                position = None
            elif hit_tp:
                pnl_pct = (tp - position['entry']) / position['entry'] if side == 'LONG' else (position['entry'] - tp) / position['entry']
                pnl = pnl_pct * DEFAULT_LEVERAGE * 20
                trades.append({'entry_time': row.name, 'side': side, 'entry': position['entry'],
                               'exit': tp, 'pnl': round(pnl, 4), 'reason': 'TP'})
                position = None
    
    # 计算统计
    if not trades:
        return None
    total_pnl = sum(t['pnl'] for t in trades)
    wins = sum(1 for t in trades if t['pnl'] > 0)
    losses = sum(1 for t in trades if t['pnl'] <= 0)
    
    return {
        'symbol': symbol, 'trades': len(trades), 'pnl': round(total_pnl, 4),
        'win_rate': round(wins/len(trades)*100, 1) if trades else 0,
        'wins': wins, 'losses': losses,
    }


def backtest_scalp(symbol: str, days: int = 3):
    """回测超短线策略（5m K线，LIMIT入场，动态止盈止损）"""
    df = fetch_klines(None, symbol, "5m", days * 288 + 200)
    if df is None or df.empty:
        return None
    
    df = compute_indicators(df)
    
    trades = []
    position = None
    
    for i in range(60, len(df)-3):
        row = df.iloc[i]
        curr = df.iloc[i]
        next_3 = df.iloc[i+1:i+4]
        
        close = float(curr['close'])
        ema9 = float(curr.get('ema9', close))
        ema21 = float(curr.get('ema21', close))
        rsi = float(curr.get('rsi', 50))
        atr = float(curr.get('atr', close * 0.005))
        vol = float(curr.get('volume', 0))
        vol_avg = float(curr.get('vol_avg', 1))
        bb_lower = float(curr.get('bb_lower', close * 0.98))
        bb_upper = float(curr.get('bb_upper', close * 1.02))
        swing_low = float(curr.get('swing_low', close * 0.99))
        swing_high = float(curr.get('swing_high', close * 1.01))
        
        trend_up = ema9 > ema21
        near_ema9 = ema9 * 0.995 <= close <= ema9 * 1.005
        vol_ratio = vol / max(vol_avg, 1)
        
        # 入场（模拟当前策略）
        if position is None:
            signal = None
            if trend_up and near_ema9 and 40 <= rsi <= 58:
                limit = max(ema9, close * 0.998)
                sl = limit * 0.9973  # 0.27%止损
                tp = limit * 1.005   # 0.5%止盈
                signal = {'side': 'LONG', 'entry': limit, 'sl': sl, 'tp': tp}
            elif not trend_up and near_ema9 and 42 <= rsi <= 60:
                limit = min(ema9, close * 1.002)
                sl = limit * 1.0027
                tp = limit * 0.995
                signal = {'side': 'SHORT', 'entry': limit, 'sl': sl, 'tp': tp}
            
            if signal:
                position = signal
        
        # 退出检查（用后面3根K线的区间）
        if position:
            high_window = max(float(c) for c in [x.get('high',0) for x in next_3]) if isinstance(next_3, list) else max(float(c['high']) for _, c in next_3.iterrows())
            low_window = min(float(c) for c in [x.get('low',float('inf')) for x in next_3]) if isinstance(next_3, list) else min(float(c['low']) for _, c in next_3.iterrows())
            
            hit_sl = (position['side'] == 'LONG' and low_window <= position['sl']) or \
                     (position['side'] == 'SHORT' and high_window >= position['sl'])
            hit_tp = (position['side'] == 'LONG' and high_window >= position['tp']) or \
                     (position['side'] == 'SHORT' and low_window <= position['tp'])
            
            if hit_sl:
                pnl = (position['sl'] - position['entry']) / position['entry'] * 3 * 10 if position['side'] == 'LONG' else \
                      (position['entry'] - position['sl']) / position['entry'] * 3 * 10
                trades.append({'side': position['side'], 'entry': position['entry'],
                               'exit': position['sl'], 'pnl': round(pnl, 4), 'reason': 'SL'})
                position = None
            elif hit_tp:
                pnl = (position['tp'] - position['entry']) / position['entry'] * 3 * 10 if position['side'] == 'LONG' else \
                      (position['entry'] - position['tp']) / position['entry'] * 3 * 10
                trades.append({'side': position['side'], 'entry': position['entry'],
                               'exit': position['tp'], 'pnl': round(pnl, 4), 'reason': 'TP'})
                position = None
    
    if not trades:
        return None
    total_pnl = sum(t['pnl'] for t in trades)
    wins = sum(1 for t in trades if t['pnl'] > 0)
    losses = sum(1 for t in trades if t['pnl'] <= 0)
    
    return {
        'symbol': symbol, 'trades': len(trades), 'pnl': round(total_pnl, 4),
        'win_rate': round(wins/len(trades)*100, 1) if trades else 0,
        'wins': wins, 'losses': losses,
    }


def run():
    """运行回测"""
    # 测试币种
    band_symbols = ['BTCUSDT', 'ETHUSDT', 'DOGEUSDT', 'AAVEUSDT', 'WLDUSDT']
    scalp_symbols = ['DOGEUSDT', 'INJUSDT', 'AAVEUSDT', 'ARBUSDT', 'WLDUSDT']
    
    print("=" * 60)
    print("📊 策略回测")
    print("=" * 60)
    
    # 波段回测
    print("\n━━━ 波段策略回测（7天1h K线）━━━")
    total_trades, total_pnl, total_wins, total_losses = 0, 0, 0, 0
    for sym in band_symbols:
        result = backtest_band(sym, 7)
        if result:
            total_trades += result['trades']
            total_pnl += result['pnl']
            total_wins += result['wins']
            total_losses += result['losses']
            icon = '✅' if result['pnl'] > 0 else '❌'
            print(f"  {icon} {sym:12s} {result['trades']:3d}笔  胜率{result['win_rate']:5.1f}%  PnL{result['pnl']:>+8.4f}U")
    
    if total_trades > 0:
        wr = total_wins / total_trades * 100
        print(f"  {'─'*50}")
        print(f"  合计: {total_trades}笔  胜率{wr:.1f}%  总PnL {total_pnl:+.4f}U")
    
    # 超短线回测
    print("\n━━━ 超短线策略回测（3天5m K线）━━━")
    total_trades, total_pnl, total_wins, total_losses = 0, 0, 0, 0
    for sym in scalp_symbols:
        result = backtest_scalp(sym, 3)
        if result:
            total_trades += result['trades']
            total_pnl += result['pnl']
            total_wins += result['wins']
            total_losses += result['losses']
            icon = '✅' if result['pnl'] > 0 else '❌'
            print(f"  {icon} {sym:12s} {result['trades']:3d}笔  胜率{result['win_rate']:5.1f}%  PnL{result['pnl']:>+8.4f}U")
    
    if total_trades > 0:
        wr = total_wins / total_trades * 100
        print(f"  {'─'*50}")
        print(f"  合计: {total_trades}笔  胜率{wr:.1f}%  总PnL {total_pnl:+.4f}U")


if __name__ == "__main__":
    run()

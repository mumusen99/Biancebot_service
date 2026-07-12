"""
Signal scoring engine for scalping strategy.

Extracted from scalper.py scan_signals() closure.
Pure computation — no exchange API calls inside the scoring logic.
All exchange data is fetched via _fetch_klines_ws (imported from scalper).

compute_signal(sym, btc_returns, bias) -> Optional[dict]
  Returns signal dict with score/tier/sl/tp or None if filtered out.
"""

import time
import logging

from trading_bot.strategy.indicators import compute_indicators

# Module globals imported from scalper (set by apply_runtime_config)
from trading_bot.strategy.scalper import (
    TIMEFRAME, KLINES_LIMIT, _ENTRY_CFG, BLOCKLIST,
    _PUMP_COOLDOWNS, _fetch_klines_ws,
)

logger = logging.getLogger(__name__)


def compute_signal(sym: str, btc_returns: dict, bias: int):
    """Score one symbol for entry. Returns signal dict or None."""
    try:
        df = _fetch_klines_ws(sym, TIMEFRAME, KLINES_LIMIT)
        if df is None or df.empty or len(df) < 30:
            return None
        df = compute_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        close = float(last['close'])
        ema9 = float(last.get('ema9', close))
        ema21 = float(last.get('ema21', close))
        rsi = float(last.get('rsi', 50))
        vol = float(last.get('volume', 0))
        vol_avg = float(last.get('vol_avg', 1))
        atr = float(last.get('atr', close * 0.005))
        swing_low = float(last.get('swing_low', close * 0.99))
        swing_high = float(last.get('swing_high', close * 1.01))
        prev_close = float(prev['close'])
        prev_ema9 = float(prev.get('ema9', prev_close))

        trend_up = ema9 > ema21
        trend_down = ema9 < ema21
        vol_ratio = vol / vol_avg if vol_avg > 0 else 1
        ema_dist_pct = abs(close - ema9) / ema9 * 100

        # ─── 15m vol 参考 ───
        try:
            df_15m = _fetch_klines_ws(sym, '15m', 30)
            if df_15m is not None and len(df_15m) >= 5:
                vol_15m = float(df_15m.iloc[-1].get('volume', 0))
                vol_15m_avg = float(df_15m['volume'].tail(20).mean()) if len(df_15m) >= 20 else vol_15m
                vol_15m_ratio = vol_15m / max(vol_15m_avg, 1)
            else:
                vol_15m_ratio = 1.0
        except Exception:
            vol_15m_ratio = 1.0

        cfg = _ENTRY_CFG

        # ─── 硬拒绝 + 防追高 ───
        if sym in BLOCKLIST:
            return None

        # 暴涨冷却检查
        now_ts = time.time()
        if sym in _PUMP_COOLDOWNS and now_ts < _PUMP_COOLDOWNS[sym]:
            remaining = int(_PUMP_COOLDOWNS[sym] - now_ts)
            if remaining > 0:
                return None  # POST_PUMP_COOLDOWN

        # 获取1m K线检测暴涨
        df_1m = None
        try:
            df_1m = _fetch_klines_ws(sym, '1m', 8)
        except Exception:
            logger.debug("1m kline fetch failed", exc_info=True)
            pass

        pump_detected = False
        pump_reason = ''
        if df_1m is not None and len(df_1m) >= 5:
            close_1m = df_1m['close'].values
            atr_ref = max(atr, close * 0.003)  # 最小0.3%波动

            pct_1m = abs(close - close_1m[-2]) / close_1m[-2] * 100 if len(close_1m) >= 2 else 0
            pct_3m = abs(close - close_1m[-4]) / close_1m[-4] * 100 if len(close_1m) >= 4 else 0
            pct_5m = abs(close - close_1m[0]) / close_1m[0] * 100 if len(close_1m) >= 5 else 0

            atr_pct = atr_ref / close * 100
            max_1m = max(atr_pct * cfg.get('max_1m_move_atr', 0.8), 0.4)
            max_3m = max(atr_pct * cfg.get('max_3m_move_atr', 1.5), 0.8)
            max_5m = max(atr_pct * cfg.get('max_5m_move_atr', 2.5), 1.2)

            if pct_1m > max_1m:
                pump_detected = True
                pump_reason = f'1m泵{pct_1m:.1f}%>{max_1m:.1f}%'
            elif pct_3m > max_3m:
                pump_detected = True
                pump_reason = f'3m泵{pct_3m:.1f}%>{max_3m:.1f}%'
            elif pct_5m > max_5m:
                pump_detected = True
                pump_reason = f'5m泵{pct_5m:.1f}%>{max_5m:.1f}%'

        if pump_detected:
            cooldown_s = cfg.get('post_pump_cooldown_minutes', 5) * 60
            _PUMP_COOLDOWNS[sym] = now_ts + cooldown_s
            return None  # POST_PUMP_COOLDOWN

        # ─── 1m M顶/W底检测（防反转陷阱）───
        m_top = False; w_bottom = False; pattern_risk = 0
        if df_1m is not None and len(df_1m) >= 6:
            highs = df_1m['high'].values[-6:]
            lows = df_1m['low'].values[-6:]
            closes = df_1m['close'].values[-6:]
            mid = closes[-1]
            # M顶: 两个几乎等高的峰，中间有低点，当前价格在回落中
            peaks = []
            for i in range(1, len(highs)-1):
                if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                    if highs[i] > mid * 1.002:
                        peaks.append((i, highs[i]))
            if len(peaks) >= 2:
                p1, p2 = peaks[-2], peaks[-1]
                p1_idx, p1_h = p1; p2_idx, p2_h = p2
                if p2_idx - p1_idx >= 2:  # 两个峰之间至少隔一根K线
                    between_lows = lows[p1_idx+1:p2_idx]
                    valley = min(between_lows) if len(between_lows) > 0 else p1_h
                    pct_diff = abs(p2_h - p1_h) / p1_h
                    valley_pct = (p1_h - valley) / p1_h
                    if pct_diff < 0.01 and valley_pct > 0.003:  # 双峰差距<1%，谷深>0.3%
                        m_top = True
                        pattern_risk = 3
            # W底: 两个几乎等高的谷，中间有峰，当前价格在反弹中
            if not m_top:
                valleys = []
                for i in range(1, len(lows)-1):
                    if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                        if lows[i] < mid * 0.998:
                            valleys.append((i, lows[i]))
                if len(valleys) >= 2:
                    v1, v2 = valleys[-2], valleys[-1]
                    v1_idx, v1_l = v1; v2_idx, v2_l = v2
                    if v2_idx - v1_idx >= 2:
                        between_highs = highs[v1_idx+1:v2_idx]
                        peak = max(between_highs) if len(between_highs) > 0 else v1_l
                        pct_diff = abs(v2_l - v1_l) / v1_l
                        peak_pct = (peak - v1_l) / v1_l
                        if pct_diff < 0.01 and peak_pct > 0.003:
                            w_bottom = True
                            pattern_risk = 3

        # VWAP/EMA20 距离过滤（宽松硬拒 + 打分）
        vwap = float(last.get('vwap', close))
        ema20 = float(last.get('ema20', close))
        dist_vwap = abs(close - vwap) / close
        dist_ema20 = abs(close - ema20) / close

        # 硬拒：极端偏离（>1.5x ATR）
        max_vwap_hard = 1.5 * (atr / close)
        max_ema20_hard = 1.2 * (atr / close)
        if dist_vwap > max_vwap_hard:
            return None  # 离VWAP太远
        if dist_ema20 > max_ema20_hard:
            return None  # 离EMA20太远

        if ema_dist_pct > cfg['ema_hard']:
            return None
        if vol_ratio < cfg['vol_reject']:
            return None

        # ─── 方向判断（BTC偏置，非唯一方向）───
        # 允许双向开仓，BTC只控制风险系数
        long_allowed = trend_up
        short_allowed = trend_down
        if not long_allowed and not short_allowed:
            return None

        side = 'LONG' if long_allowed else 'SHORT'
        if long_allowed and short_allowed:
            side = 'LONG' if bias >= 0 else 'SHORT'

        # ─── 强单边禁止反向 ───
        if side == 'LONG' and bias <= -4:
            return None
        if side == 'SHORT' and bias >= 4:
            return None

        # BTC方向偏置：同向正常，反向降低风险
        btc_align = (side == 'LONG' and bias >= 0) or (side == 'SHORT' and bias <= 0)
        direction_risk_factor = 1.0 if btc_align else 0.6

        if side == 'LONG':
            rsi_ideal = cfg['rsi_l_ideal']
            rsi_ok = cfg['rsi_l_ok']
        else:
            rsi_ideal = cfg['rsi_s_ideal']
            rsi_ok = cfg['rsi_s_ok']

        score = 0.0

        # 1. 趋势结构 (max 4.0)
        if trend_up:
            if close > ema21: score += 1.0  # 价格在EMA21之上
            if ema9 > ema21: score += 1.0  # EMA9在EMA21之上
        else:
            if close < ema21: score += 1.0
            if ema9 < ema21: score += 1.0
        # 短周期趋势确认 (前一根K线也同向)
        if prev_ema9 > ema21 and ema9 > ema21:
            score += 1.0  # 持续多头
        elif prev_ema9 < ema21 and ema9 < ema21:
            score += 1.0  # 持续空头
        else:
            score += 0.5  # 刚转向
        if ema_dist_pct <= cfg['ema_max']:
            score += 1.0  # 价格在合理趋势区域内

        # 2. EMA回踩质量 (max 2.0)
        if ema_dist_pct <= cfg['ema_ideal']:
            score += 2.0
        elif ema_dist_pct <= cfg['ema_normal']:
            score += 1.5
        elif ema_dist_pct <= cfg['ema_max']:
            score += 1.0

        # 3. RSI (max 2.0)
        if rsi_ideal[0] <= rsi <= rsi_ideal[1]:
            score += 2.0
        elif rsi_ok[0] <= rsi <= rsi_ok[1]:
            score += 1.0
        elif rsi > rsi_ok[1] - 2 and rsi <= rsi_ok[1] + 2:
            score += 0.5
        elif rsi < rsi_ok[0] + 2 and rsi >= rsi_ok[0] - 2:
            score += 0.5

        # 3b. MACD (max 2.5, 仅加分不扣分)
        try:
            macd = float(last.get('macd', 0))
            macd_sig = float(last.get('macd_signal', 0))
            macd_hist = float(last.get('macd_histogram', 0))
            prev_macd = float(prev.get('macd', 0))
            prev_sig = float(prev.get('macd_signal', 0))
            # 金叉
            if prev_macd <= prev_sig and macd > macd_sig:
                score += 1.5
            # 零轴上方
            if macd > 0:
                score += 0.5
            # 柱状图放大
            if abs(macd_hist) > abs(float(prev.get('macd_histogram', 0))):
                score += 0.5
        except Exception:
            pass

        # 4. 当前K线确认 (max 2.5)
        if side == 'LONG':
            if close > prev_close: score += 1.0
            if close > prev_ema9: score += 1.0
        else:
            if close < prev_close: score += 1.0
            if close < prev_ema9: score += 1.0
        # 影线确认
        low_p = float(last.get('low', close))
        high_p = float(last.get('high', close))
        if side == 'LONG' and (close - low_p) > (high_p - close) * 0.5:
            score += 0.5  # 下影线明显
        elif side == 'SHORT' and (high_p - close) > (close - low_p) * 0.5:
            score += 0.5  # 上影线明显

        # 5. 成交量 (max 2.0)
        if vol_ratio >= cfg['vol_strong']:
            score += 2.0
        elif vol_ratio >= cfg['vol_normal']:
            score += 1.5
        elif vol_ratio >= cfg['vol_reject']:
            score += 1.0

        # 5b. 15m vol 参考 (max ±1.0)
        if vol_15m_ratio > 1.5:
            score += 1.0   # 15m 放量，市场活跃
        elif vol_15m_ratio > 1.2:
            score += 0.5
        elif vol_15m_ratio < 0.4:
            score -= 1.0   # 15m 缩量，冷清市场扣分
        elif vol_15m_ratio < 0.6:
            score -= 0.5

        # 6. BTC环境 (max 1.5)
        if side == 'LONG' and bias >= 4: score += 1.5
        elif side == 'LONG' and bias >= 1: score += 0.5
        elif side == 'SHORT' and bias <= -4: score += 1.5
        elif side == 'SHORT' and bias <= -1: score += 0.5
        else: score += 0.5  # BTC中性

        # 7. 相对强度 vs BTC (max 1.0)
        # 计算山寨币自身 1m/3m/5m 收益，与 BTC 对比
        rs_score = 0.0
        rs_3m = 0.0
        rs_5m = 0.0
        if btc_returns:
            try:
                coin_1m = _fetch_klines_ws(sym, '1m', 10)
                if coin_1m is not None and len(coin_1m) >= 8:
                    coin_close = coin_1m['close'].values
                    coin_now = float(coin_close[-1])
                    coin_1m_ret = (coin_now - float(coin_close[-2])) / float(coin_close[-2]) * 100 if len(coin_close) >= 2 else 0
                    coin_3m_ret = (coin_now - float(coin_close[-4])) / float(coin_close[-4]) * 100 if len(coin_close) >= 4 else 0
                    coin_5m_ret = (coin_now - float(coin_close[0])) / float(coin_close[0]) * 100 if len(coin_close) >= 5 else 0

                    rs_3m = coin_3m_ret - btc_returns.get('3m', 0)
                    rs_5m = coin_5m_ret - btc_returns.get('5m', 0)

                    if rs_3m > 0:
                        rs_score += 1.0
                    if rs_5m > 0:
                        rs_score += 0.5
            except Exception:
                logger.debug("relative strength calc failed", exc_info=True)
                pass

        # 硬拒绝：BTC 涨但山寨跌（弱势山寨，不做多）
        if side == 'LONG' and btc_returns and btc_returns.get('3m', 0) > 0.05 and rs_3m < -0.30:
            return None  # BTC上涨但山寨显著落后
        if side == 'SHORT' and btc_returns and btc_returns.get('3m', 0) < -0.05 and rs_3m > 0.30:
            return None  # BTC下跌但山寨显著抗跌

        score += rs_score

        # 8. 支撑区质量 (max 2.0) — 禁止EMA9单独当支撑
        # 价格必须至少靠近 VWAP 或 EMA20 中的至少一个
        atr_pct = atr / close
        near_vwap = dist_vwap <= 0.5 * atr_pct
        near_ema20 = dist_ema20 <= 0.4 * atr_pct
        near_swing = (close - swing_low) / close <= 1.5 * atr_pct if side == 'LONG' else (swing_high - close) / close <= 1.5 * atr_pct

        if near_vwap: score += 0.8
        if near_ema20: score += 0.7
        # VWAP和EMA20收敛（两者距离 < 0.25*ATR）
        if abs(vwap - ema20) / close < 0.25 * atr_pct:
            score += 0.5
        # 硬拒绝：EMA9距离很近但VWAP/EMA20都很远 = 假支撑
        if ema_dist_pct <= 0.15 and not near_vwap and not near_ema20:
            score -= 1.5  # EMA9孤立，不是可靠支撑

        # 硬拒绝：离前高太近（没有上涨空间）
        if side == 'LONG':
            dist_from_high = (swing_high - close) / close
            if dist_from_high < 0.3 * atr_pct:
                return None  # 太接近前高，不追
        else:
            dist_from_low = (close - swing_low) / close
            if dist_from_low < 0.3 * atr_pct:
                return None  # 太接近前低，不追

        # ─── 位置百分位 + 极值惩罚 ───
        pos_pct = (close - swing_low) / (swing_high - swing_low) if swing_high > swing_low else 0.5
        extreme_penalty = 0
        if side == 'LONG':
            if pos_pct > 0.90: extreme_penalty = 20
            elif pos_pct > 0.80: extreme_penalty = 12
            elif pos_pct > 0.70: extreme_penalty = 5
        else:
            if pos_pct < 0.10: extreme_penalty = 20
            elif pos_pct < 0.20: extreme_penalty = 12
            elif pos_pct < 0.30: extreme_penalty = 5

        # ─── VWAP偏离硬限制 ───
        vwap_dev_atr = (close - vwap) / atr if atr > 0 else 0
        if side == 'LONG' and vwap_dev_atr > 1.2:
            return None  # 价格远高于VWAP，不追多
        if side == 'SHORT' and vwap_dev_atr < -1.2:
            return None  # 价格远低于VWAP，不追空

        # ─── 动量衰竭检测 ───
        momentum_exhausted = False

        # ─── 区间中部禁开（震荡时更严格）───
        if 0.35 < pos_pct < 0.65:
            extreme_penalty += 8  # 区间中部，额外扣分

        # ─── 回踩质量评估 ───
        pullback_bars = 0
        push_bars = 0
        pullback_vol = 0.0
        push_vol = 0.0
        for i in range(-8, 0):
            ci = float(df.iloc[i]['close'])
            pi = float(df.iloc[i-1]['close']) if i > -len(df) else ci
            vi = float(df.iloc[i]['volume'])
            if ci < pi:
                pullback_bars += 1
                pullback_vol += vi
            else:
                push_bars += 1
                push_vol += vi

        min_pb_bars = cfg.get('min_pullback_bars', 2)
        max_pb_bars = cfg.get('max_pullback_bars', 8)

        if pullback_bars < min_pb_bars:
            score -= 1.5  # 回调太短
        elif pullback_bars > max_pb_bars:
            score -= 1.0  # 回调太久，趋势可能失效

        # 回撤缩量检查
        if pullback_bars > 0 and push_bars > 0:
            pb_vol_avg = pullback_vol / pullback_bars
            push_vol_avg = push_vol / push_bars
            if push_vol_avg > 0:
                pb_vol_ratio = pb_vol_avg / push_vol_avg
                if pb_vol_ratio < cfg.get('max_pullback_volume_ratio', 0.70):
                    score += 1.0  # 健康缩量
                elif pb_vol_ratio > 1.2:
                    score -= 1.0  # 放量回调，可能是抛压

        # Higher Low检测
        recent_lows = []
        for i in range(-10, 0):
            li = float(df.iloc[i]['low'])
            recent_lows.append(li)
        hl_count = 0
        for i in range(1, len(recent_lows)-1):
            if recent_lows[i] > recent_lows[i+1]:
                hl_count += 1
        if hl_count >= 2:
            score += 1.0  # 连续Higher Low

        # ─── 动量衰竭检测（回踩分析完成后）───
        if not momentum_exhausted and pullback_bars > 0 and push_bars > 0:
            if vol_ratio < 0.6 and side == 'LONG' and close > prev_close:
                momentum_exhausted = True
            elif vol_ratio < 0.6 and side == 'SHORT' and close < prev_close:
                momentum_exhausted = True

        if momentum_exhausted:
            score -= 3.0

        # ─── 极值位置惩罚 + 区间中部 ───
        score -= extreme_penalty

        # ─── 入场分层 ───
        # 评分≥7.0 + 位置有利(20%~65%LONG/35%~80%SHORT) → 市价
        # 评分≥7.0 + 位置不利 → 激进LIMIT(30s过期)
        # 评分≥6.0 → 激进LIMIT(30s)
        # 评分≥4.5 → 被动LIMIT(90s)
        tier = 'skip'
        if score >= cfg['market']:
            if side == 'LONG' and pos_pct > 0.65:
                tier = 'aggressive'  # 位置太右，降级防追高
            elif side == 'SHORT' and pos_pct < 0.35:
                tier = 'aggressive'
            else:
                tier = 'market'
        if tier == 'skip' and score >= cfg['aggressive']:
            tier = 'aggressive'
        if tier == 'skip' and score >= cfg['limit']:
            tier = 'limit'

        if tier == 'skip':
            return None  # 不够最低门槛

        # ─── 结构止损计算（多锚点）───
        # 计算回撤最低点（用于止损锚定）
        pullback_low = close
        pullback_high = close
        for i in range(-pullback_bars, 0):
            li = float(df.iloc[i]['low'])
            hi = float(df.iloc[i]['high'])
            if li < pullback_low: pullback_low = li
            if hi > pullback_high: pullback_high = hi

        if side == 'LONG':
            limit_price = max(ema9, close * 0.998)
            # 结构止损: 取最保守锚点
            sl_buf_5m = cfg.get('atr_5m_buffer', 0.25)
            min_buf = cfg.get('minimum_buffer_pct', 0.001) * close
            atr_buf = max(atr * sl_buf_5m, min_buf)
            structural_sl = min(pullback_low, swing_low, ema21 - atr * 0.20)
            sl_price = structural_sl - atr_buf
            sl_price = round(sl_price, 8)
            risk_dist = abs(limit_price - sl_price)
            tp_raw = max(swing_high, limit_price + risk_dist * 2)
            tp_price = round(min(max(tp_raw, limit_price + risk_dist * 1.5), limit_price * 1.03), 8)
        else:
            limit_price = min(ema9, close * 1.001)
            sl_buf_5m = cfg.get('atr_5m_buffer', 0.25)
            min_buf = cfg.get('minimum_buffer_pct', 0.001) * close
            atr_buf = max(atr * sl_buf_5m, min_buf)
            structural_sl = max(pullback_high, swing_high, ema21 + atr * 0.20)
            sl_price = structural_sl + atr_buf
            sl_price = round(sl_price, 8)
            risk_dist = abs(sl_price - limit_price)
            tp_raw = min(swing_low, limit_price - risk_dist * 2)
            tp_price = round(max(min(tp_raw, limit_price - risk_dist * 1.5), limit_price * 0.97), 8)

        detail = f'EMA{ema_dist_pct:.1f}% VWAP{dist_vwap*100:.1f}% PB{pullback_bars}b RSI{rsi:.0f}'
        early = score < cfg['aggressive'] and cfg['early_enabled']
        mode = 'early' if early else ('confirmed' if score >= cfg['aggressive'] else 'momentum')
        emoji = '🟢' if side == 'LONG' else '🔴'

        logger.info(f'  {emoji} {sym} {side} [{tier}|{mode}] sc={score:.1f} {detail}')

        # ─── 四维子分 ───
        # direction_score (0-10): 趋势结构 + BTC/ETH + RS + 成交量方向
        dir_score = 0.0
        # 趋势结构部分 (来自原始评分项1, max 4)
        if side == 'LONG':
            if close > ema21: dir_score += 1.0
            if ema9 > ema21: dir_score += 1.0
        else:
            if close < ema21: dir_score += 1.0
            if ema9 < ema21: dir_score += 1.0
        if ema_dist_pct <= cfg['ema_max']: dir_score += 1.0
        # BTC bonus (max 1.5)
        if (side=='LONG' and bias>=4) or (side=='SHORT' and bias<=-4): dir_score += 1.5
        elif (side=='LONG' and bias>=1) or (side=='SHORT' and bias<=-1): dir_score += 0.5
        else: dir_score += 0.5
        # RS (max 1.5)
        dir_score += min(1.5, rs_score)
        # 成交量方向
        if vol_ratio >= cfg['vol_strong']: dir_score += 1.5
        elif vol_ratio >= cfg['vol_normal']: dir_score += 1.0
        elif vol_ratio >= cfg['vol_reject']: dir_score += 0.5
        dir_score = round(min(10, dir_score), 1)

        # location_score (0-10): 位置百分位 + VWAP偏离 + 支撑距离 + 空间
        loc_score = 0.0
        # 位置百分位 (max 2.5)
        if side == 'LONG':
            if pos_pct <= 0.20: loc_score += 2.5
            elif pos_pct <= 0.35: loc_score += 2.0
            elif pos_pct <= 0.50: loc_score += 1.0
        else:
            if pos_pct >= 0.80: loc_score += 2.5
            elif pos_pct >= 0.65: loc_score += 2.0
            elif pos_pct >= 0.50: loc_score += 1.0
        # VWAP偏离 (max 2.0)
        vwap_dist_pct = abs(close-vwap)/close*100
        if vwap_dist_pct <= 0.3: loc_score += 2.0
        elif vwap_dist_pct <= 0.6: loc_score += 1.0
        elif vwap_dist_pct <= 1.0: loc_score += 0.5
        # 支撑/压力距离 (max 2.0)
        if near_vwap: loc_score += 1.0
        if near_ema20: loc_score += 1.0
        # Z-score位置 (max 1.5)
        sma20 = float(last.get('sma20', close))
        bb_std = float(last.get('bb_std', atr))
        z = (close - sma20) / bb_std if bb_std > 0 else 0
        if side == 'LONG' and z < -0.5: loc_score += 1.5
        elif side == 'LONG' and z < 0: loc_score += 0.8
        elif side == 'SHORT' and z > 0.5: loc_score += 1.5
        elif side == 'SHORT' and z > 0: loc_score += 0.8
        # 边界触碰次数 (max 1.0) — 简化：首次靠近给满分
        if abs(close - swing_low) / close < atr_pct if side=='LONG' else abs(swing_high - close) / close < atr_pct:
            loc_score += 1.0
        loc_score = round(min(10, loc_score), 1)

        # trigger_score (0-10): K线确认 + 回踩质量 + Higher Low + 缩量
        trig_score = 0.0
        # K线确认 (max 2.0, from original items 4)
        if side == 'LONG':
            if close > prev_close: trig_score += 1.0
            if close > prev_ema9: trig_score += 1.0
        else:
            if close < prev_close: trig_score += 1.0
            if close < prev_ema9: trig_score += 1.0
        # 回踩质量 (max 2.0)
        if pullback_bars >= 2 and pullback_bars <= 6: trig_score += 2.0
        elif pullback_bars > 0: trig_score += 1.0
        # Higher Low (max 1.5)
        if hl_count >= 2: trig_score += 1.5
        elif hl_count >= 1: trig_score += 0.8
        # 回踩缩量 (max 1.5)
        if pullback_bars > 0 and push_bars > 0:
            pb_vol_ratio2 = (pullback_vol/pullback_bars) / (push_vol/push_bars) if push_vol > 0 else 999
            if pb_vol_ratio2 < 0.55: trig_score += 1.5
            elif pb_vol_ratio2 < 0.75: trig_score += 1.0
            elif pb_vol_ratio2 < 1.0: trig_score += 0.5
        # RSI拐头 (max 1.0)
        if side == 'LONG' and rsi > float(prev.get('rsi', rsi)): trig_score += 1.0
        elif side == 'SHORT' and rsi < float(prev.get('rsi', rsi)): trig_score += 1.0
        trig_score = round(min(10, trig_score), 1)

        # execution_score (0-10): 止损合理 + 箱体合适
        exec_score = 0.0
        stop_pct = abs(sl_price - limit_price) / limit_price * 100
        if 0.45 <= stop_pct <= 0.85: exec_score += 4.0
        elif 0.45 <= stop_pct <= 1.10: exec_score += 2.0
        # 盈亏比 (max 3.0)
        rr = abs(tp_price - limit_price) / max(0.001, abs(sl_price - limit_price))
        if rr >= 1.8: exec_score += 3.0
        elif rr >= 1.4: exec_score += 2.0
        elif rr >= 1.0: exec_score += 1.0
        # 位置合适 (max 3.0)
        if side == 'LONG' and pos_pct <= 0.50: exec_score += 3.0
        elif side == 'LONG' and pos_pct <= 0.65: exec_score += 1.5
        elif side == 'SHORT' and pos_pct >= 0.50: exec_score += 3.0
        elif side == 'SHORT' and pos_pct >= 0.35: exec_score += 1.5
        exec_score = round(min(10, exec_score), 1)

        # ─── 1m形态风险加减分 ───
        if pattern_risk > 0:
            if m_top and side == 'LONG':
                score -= pattern_risk
                detail += ' M顶!'
            elif m_top and side == 'SHORT':
                score += 2  # M顶确认空头方向
                detail += ' M顶✅'
            if w_bottom and side == 'SHORT':
                score -= pattern_risk
                detail += ' W底!'
            elif w_bottom and side == 'LONG':
                score += 2  # W底确认多头方向
                detail += ' W底✅'

        return {
            'symbol': sym, 'side': side, 'score': round(score, 1),
            'tier': tier, 'mode': mode, 'rsi': round(rsi, 1),
            'reason': f'{tier}/{mode} {detail}',
            'limit_price': round(limit_price, 8),
            'sl_price': sl_price, 'tp_price': tp_price,
            'early': early,
            'dist_vwap': round(dist_vwap * 100, 2),
            'dist_ema20': round(dist_ema20 * 100, 2),
            'pullback_bars': pullback_bars,
            'direction_risk_factor': direction_risk_factor,
            'pos_pct': round(pos_pct, 3),
            'dir_score': dir_score,
            'loc_score': loc_score,
            'trig_score': trig_score,
            'exec_score': exec_score,
        }
    except Exception:
        pass
    return None


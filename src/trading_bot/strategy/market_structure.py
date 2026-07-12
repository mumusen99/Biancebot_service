#!/usr/bin/env python3
"""
全局市场结构保护层
==================
超短线策略的上层风控模块，基于 BTC/ETH 结构、市场宽度、摆动点确认，
判断当前市场状态，控制可开仓方向和风险系数。

文档: docs/market_structure_protection.md

等级说明:
  MONITOR = 只记录日志，不实际阻止交易（实盘验证阶段用）
  ACTIVE = 正常阻止/放行交易（灰度启用后使用）
"""
import json, time, logging, math, os
from pathlib import Path
from datetime import datetime, timezone
from trading_bot.core.env_config import get_exchange_config
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

import pandas as pd
import requests as req

from trading_bot.core.settings import API_KEY, API_SECRET, PROXY, BOT_STATE_FILE
from trading_bot.exchange.market_data import fetch_klines

logger = logging.getLogger("mkt_struct")


def _trade_time(t: dict) -> float:
    """提取交易的Unix时间戳"""
    try:
        return datetime.fromisoformat(t.get("time","")).timestamp()
    except:
        return 0

# ═══════════════════════════════════════════════════
#  枚举 & 常量
# ═══════════════════════════════════════════════════

class MarketRegime(str, Enum):
    """市场状态（按优先级从高到低）"""
    SYSTEM_HALT   = "SYSTEM_HALT"
    DATA_INVALID  = "DATA_INVALID"
    LOSS_COOLDOWN = "LOSS_COOLDOWN"
    GLOBAL_LONG_FREEZE = "GLOBAL_LONG_FREEZE"
    RISK_OFF      = "RISK_OFF"
    DISTRIBUTION  = "DISTRIBUTION"
    PROFIT_LOCK   = "PROFIT_LOCK"
    PULLBACK      = "PULLBACK"
    CHOP          = "CHOP"
    RISK_ON       = "RISK_ON"

class SwingType(str, Enum):
    CANDIDATE  = "candidate"
    CONFIRMED  = "confirmed"

class Direction(str, Enum):
    LONG  = "long"
    SHORT = "short"
    NEUTRAL = "neutral"

class Mode(str, Enum):
    MONITOR = "monitor"  # 只记录日志
    ACTIVE  = "active"   # 实际阻止

# ─── 状态最短保持时间（秒） ────────────────────────────
MIN_HOLD_TIME = {
    MarketRegime.RISK_ON:       300,  # 5 min
    MarketRegime.PULLBACK:      180,  # 3 min
    MarketRegime.CHOP:          300,  # 5 min
    MarketRegime.DISTRIBUTION:  300,  # 5 min
    MarketRegime.RISK_OFF:      600,  # 10 min
    MarketRegime.GLOBAL_LONG_FREEZE: 300,
    MarketRegime.PROFIT_LOCK:   300,
    MarketRegime.LOSS_COOLDOWN: 600,
    MarketRegime.DATA_INVALID:  60,
    MarketRegime.SYSTEM_HALT:   999999,
}

# 紧急状态：必须立即生效，不受最短保持时间限制
IMMEDIATE_REGIMES = {
    MarketRegime.SYSTEM_HALT,
    MarketRegime.DATA_INVALID,
    MarketRegime.LOSS_COOLDOWN,
    MarketRegime.GLOBAL_LONG_FREEZE,
    MarketRegime.RISK_OFF,
}

# ATR 阈值
ATR_5M_PERIOD = 14
ATR_1M_PERIOD = 14
MIN_PCT_THRESHOLD = 0.0005  # 0.05%

# 市场宽度
TOP_N = 50
BREADTH_SMOOTH_PERIOD = 4  # ~4 min EMA

# 数据过期
MAX_STATE_AGE = 60       # 全局快照最多 60 秒
MAX_KLINE_DELAY = 90     # K 线延迟超过 90 秒则禁止开仓

# ═══════════════════════════════════════════════════
#  数据结构
# ═══════════════════════════════════════════════════

@dataclass
class SwingPoint:
    price: float
    time: float
    swing_type: SwingType = SwingType.CANDIDATE

@dataclass
class MarketSnapshot:
    """全局市场快照（同一轮扫描中所有币共用）"""
    version: int
    generated_at: float
    btc_state: dict
    eth_state: dict
    regime: MarketRegime
    breadth_raw: float
    breadth_smooth: float
    long_freeze: bool
    loss_cooldown: bool
    profit_lock: bool
    long_factor: float
    short_factor: float
    data_valid: bool
    primary_reason: str = ""
    secondary_reasons: list = field(default_factory=list)

# ═══════════════════════════════════════════════════
#  全局状态管理
# ═══════════════════════════════════════════════════

class MarketStateManager:
    """全局市场结构状态机"""
    
    def __init__(self, mode: Mode = Mode.MONITOR):
        self.mode = mode
        self._snapshot_counter = 0
        
        self.btc_swing_highs: list[SwingPoint] = []
        self.btc_swing_lows: list[SwingPoint] = []
        self.eth_swing_highs: list[SwingPoint] = []
        self.eth_swing_lows: list[SwingPoint] = []
        
        self.current_regime = MarketRegime.RISK_ON
        self.prev_regime = MarketRegime.RISK_ON
        self.regime_changed_at = time.time()
        self.long_frozen = False
        self.long_freeze_reason = ""
        self._last_snapshot: Optional[MarketSnapshot] = None
        self._last_snapshot_time = 0
        self.loss_cooldown = False
        self._loss_streak = 0
        self._loss_start_time = 0
        self._profit_lock_active = False
        self._profit_lock_start = 0
        self._top50_cache = None
        self._top50_cache_time = 0
        
        # 状态历史（用于日志/复盘）
        self.regime_history: list[dict] = []
        
    # ─── 数据源 ──────────────────────────────────
    
    def _fetch_klines(self, symbol: str, tf: str, limit: int = 100) -> pd.DataFrame:
        """获取 K 线并校验时间连续性"""
        df = fetch_klines(symbol=symbol, timeframe=tf, limit=limit)
        if df is not None and not df.empty:
            df["time_ms"] = df["timestamp"].astype("int64") // 10**6  # 毫秒时间戳
        if df is None or len(df) < 20:
            return None
        # 检查最后一条 K 线是否过期
        last_time = int(df['timestamp'].iloc[-1].timestamp() * 1000) if hasattr(df['timestamp'].iloc[-1], 'timestamp') else int(df['timestamp'].iloc[-1])
        if tf == '1m':
            expected_close = (last_time // 60000 + 1) * 60000
        elif tf == '5m':
            expected_close = (last_time // 300000 + 1) * 300000
        else:
            expected_close = last_time + 60000
        
        delay = time.time() * 1000 - expected_close
        if delay > MAX_KLINE_DELAY * 1000:
            logger.warning(f'   ⏰ {symbol} {tf} K线延迟 {delay/1000:.0f}s')
            return None
        return df
    
    def _fetch_all_tickers(self) -> dict:
        """获取所有 USDT 永续合约最新价"""
        try:
            r = req.get(f'{get_exchange_config().fapi_v1_base}/ticker/price',
                         proxies={'http': PROXY, 'https': PROXY}, timeout=10)
            if r.status_code == 200:
                return {x['symbol']: float(x['price']) for x in r.json()
                        if x['symbol'].endswith('USDT')}
        except:
            pass
        return {}
    
    # ─── 技术指标 ──────────────────────────────────
    
    def _ema(self, arr, period):
        a = 2 / (period + 1)
        r = [arr[0]]
        for v in arr[1:]:
            r.append(v * a + r[-1] * (1 - a))
        return r
    
    def _atr(self, df: pd.DataFrame, period: int) -> float:
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        if len(close) < period + 1:
            return 0
        tr = []
        for i in range(1, len(close)):
            hl = high[i] - low[i]
            hc = abs(high[i] - close[i-1])
            lc = abs(low[i] - close[i-1])
            tr.append(max(hl, hc, lc))
        if len(tr) < period:
            return sum(tr) / len(tr) if tr else 0
        return sum(tr[-period:]) / period
    
    def _ema20_slope(self, close: list, period: int = 20) -> float:
        """EMA20 斜率（点数/根）"""
        if len(close) < period + 5:
            return 0
        ema = self._ema(close, period)
        # 最近 5 根的变化斜率
        return (ema[-1] - ema[-5]) / 5
    
    # ─── 摆动点管理 ──────────────────────────────────
    
    def _update_swing_points(self, df_5m: pd.DataFrame):
        """更新 BTC 5m 摆动点（候选→确认），带时间戳去重"""
        high = df_5m['high'].values
        low = df_5m['low'].values
        close = df_5m['close'].values
        times = df_5m['time_ms'].values  # 时间戳
        atr5 = self._atr(df_5m, 14)
        min_move = max(atr5 * 0.2, close[-1] * MIN_PCT_THRESHOLD)
        
        # 已有时间戳集合（用于去重）
        existing_high_times = {sp.time for sp in self.btc_swing_highs}
        existing_low_times = {sp.time for sp in self.btc_swing_lows}
        
        n = len(close)
        # 找局部高点（3根K线中最高），带时间戳去重
        for i in range(1, n - 1):
            if high[i] > high[i-1] and high[i] > high[i+1]:
                t = times[i]
                if t not in existing_high_times:
                    self.btc_swing_highs.append(SwingPoint(high[i], t))
                    existing_high_times.add(t)
        
        # 找局部低点
        for i in range(1, n - 1):
            if low[i] < low[i-1] and low[i] < low[i+1]:
                t = times[i]
                if t not in existing_low_times:
                    self.btc_swing_lows.append(SwingPoint(low[i], t))
                    existing_low_times.add(t)
        
        # 确认摆动点：只使用摆动点之后的 K 线
        confirm_threshold = max(atr5 * 0.5, min_move)
        self._confirm_swings_post(df_5m, self.btc_swing_highs, confirm_threshold, is_high=True)
        self._confirm_swings_post(df_5m, self.btc_swing_lows, confirm_threshold, is_high=False)
        
        # 只保留最近 50 个
        for lst in [self.btc_swing_highs, self.btc_swing_lows,
                    self.eth_swing_highs, self.eth_swing_lows]:
            while len(lst) > 50:
                lst.pop(0)
    
    def _confirm_swings_post(self, df: pd.DataFrame, swings: list,
                              threshold: float, is_high: bool):
        """将 candidate 升级为 confirmed，只使用摆动点之后的数据"""
        close_arr = df['close'].values
        # Convert timestamp to ms integers for comparison with SwingPoint.time
        ts_col = df['timestamp']
        if hasattr(ts_col.iloc[0], 'timestamp'):
            time_arr = (ts_col.astype('int64') // 10**6).values
        else:
            time_arr = ts_col.values.astype('int64')
        
        for sp in swings:
            if sp.swing_type == SwingType.CONFIRMED:
                continue
            # 找到摆动点之后的索引
            later_indices = [j for j, t in enumerate(time_arr) if t > sp.time]
            if not later_indices:
                continue
            
            later_close = [close_arr[j] for j in later_indices]
            
            if is_high:
                # 高点确认：后续价格跌下去
                if any(c < sp.price - threshold for c in later_close):
                    sp.swing_type = SwingType.CONFIRMED
            else:
                # 低点确认：后续价格弹上去
                if any(c > sp.price + threshold for c in later_close):
                    sp.swing_type = SwingType.CONFIRMED
    
    def _get_confirmed_high(self) -> Optional[float]:
        """最近一个 confirmed 高点"""
        for sp in reversed(self.btc_swing_highs):
            if sp.swing_type == SwingType.CONFIRMED:
                return sp.price
        return None
    
    def _get_confirmed_low(self) -> Optional[float]:
        """最近一个 confirmed 低点"""
        for sp in reversed(self.btc_swing_lows):
            if sp.swing_type == SwingType.CONFIRMED:
                return sp.price
        return None
    
    def _has_lower_high(self) -> bool:
        """连续两个 confirmed Lower High"""
        highs = [sp.price for sp in self.btc_swing_highs 
                 if sp.swing_type == SwingType.CONFIRMED]
        if len(highs) >= 2:
            return highs[-1] < highs[-2] and highs[-2] < highs[-3] if len(highs) >= 3 else highs[-1] < highs[-2]
        return False
    
    def _has_higher_low(self) -> bool:
        """连续两个 confirmed Higher Low"""
        lows = [sp.price for sp in self.btc_swing_lows
                if sp.swing_type == SwingType.CONFIRMED]
        if len(lows) >= 2:
            return lows[-1] > lows[-2]
        return False
    
    def _has_lower_low(self) -> bool:
        """连续两个 confirmed Lower Low"""
        lows = [sp.price for sp in self.btc_swing_lows
                if sp.swing_type == SwingType.CONFIRMED]
        if len(lows) >= 2:
            return lows[-1] < lows[-2]
        return False
    
    # ─── 市场宽度 ──────────────────────────────────
    
    def _calc_breadth(self) -> tuple:
        """计算 Top50 币种在 5m EMA20 上方的比例"""
        tickers = self._fetch_all_tickers()
        if not tickers:
            return -1.0, -1.0  # 数据无效哨兵，调用方进入 DATA_INVALID
        
        # 排除稳定币/杠杆代币
        def _is_allowed_symbol(sym: str) -> bool:
            """只检查基础资产，不检查完整交易对字符串"""
            base = sym[:-4] if sym.endswith('USDT') else sym
            if base in ('USDC', 'BUSD', 'DAI', 'FDUSD', 'TUSD'):
                return False
            if base.endswith(('UP', 'DOWN', 'BULL', 'BEAR')):
                return False
            return True
        
        candidates = [s for s in tickers if s in self._refresh_top50()]
        
        above = 0
        valid = 0
        for sym in candidates[:TOP_N * 2]:  # 多取一些确保有 TOP_N 有效
            df = self._fetch_klines(sym, '5m', limit=30)
            if df is None or len(df) < 22:
                continue
            ema20 = self._ema(df['close'].values, 20)[-1]
            current = tickers[sym]
            if current > ema20:
                above += 1
            valid += 1
            if valid >= TOP_N:
                break
        
        raw = above / max(1, valid)
        # 平滑
        if not hasattr(self, '_breadth_ema'):
            self._breadth_ema = raw
        else:
            a = 2 / (BREADTH_SMOOTH_PERIOD + 1)
            self._breadth_ema = raw * a + self._breadth_ema * (1 - a)
        
        if valid < 40:
            logger.warning(f'  市场宽度有效样本不足: {valid}/50')
            return -1.0, -1.0  # 有效样本不足，禁止新开仓
        return raw, self._breadth_ema
    
    # ─── 市场状态判定 ──────────────────────────────
    
    def _compute_distribution_score(self, btc_5m_close, btc_ema20_slope, 
                                     breadth_smooth, btc_5m_vol) -> int:
        """DISTRIBUTION 评分制"""
        score = 0
        if self._has_lower_high():
            score += 2
        if btc_ema20_slope < 0:
            score += 1
        if breadth_smooth < 0.45:
            score += 1
        return score
    
    def _evaluate_regime(self, precomputed_breadth=None) -> tuple:
        """
        评估当前市场状态。
        返回 (regime, primary_reason, secondary_reasons)
        """
        reasons = []
        if precomputed_breadth is None:
            breadth_raw, breadth_smooth = self._calc_breadth()
        else:
            breadth_raw, breadth_smooth = precomputed_breadth
        
        # 获取 BTC 数据
        btc_5m = self._fetch_klines('BTCUSDT', '5m', limit=60)
        btc_1m = self._fetch_klines('BTCUSDT', '1m', limit=30)
        eth_5m = self._fetch_klines('ETHUSDT', '5m', limit=60)
        eth_1m = self._fetch_klines('ETHUSDT', '1m', limit=30)
        if btc_5m is None or btc_1m is None:
            # 数据失效立即进入 DATA_INVALID
            new_regime = MarketRegime.DATA_INVALID
            return new_regime, "BTC_DATA_MISSING", reasons
        
        btc_close = btc_5m['close'].values
        btc_high = btc_5m['high'].values
        btc_low = btc_5m['low'].values
        btc_vol = btc_5m['volume'].values
        btc_current = btc_close[-1]
        btc_ema20 = self._ema(btc_close, 20)[-1]
        ema20_slope = self._ema20_slope(list(btc_close))
        atr5 = self._atr(btc_5m, 14)
        min_move = max(atr5 * 0.2, btc_current * MIN_PCT_THRESHOLD)
        
        confirmed_low = self._get_confirmed_low()
        confirmed_high = self._get_confirmed_high()
        
        # 检查是否有足够时间保持当前状态
        time_in_state = time.time() - self.regime_changed_at
        min_hold = MIN_HOLD_TIME.get(self.current_regime, 0)
        can_change = time_in_state >= min_hold
        
        # 市场宽度无效时立即进入 DATA_INVALID，不能伪装为极弱市场
        if breadth_smooth is None or breadth_smooth < 0:
            return MarketRegime.DATA_INVALID, "BREADTH_DATA_INVALID", reasons

        # ─── 检查是否可以升级（允许立即升级但降级需要确认）
        
        # 1. 结构破坏检查 → RISK_OFF / GLOBAL_LONG_FREEZE
        structure_broken = False
        if confirmed_low:
            # 跌破前低
            breakdown = btc_current < confirmed_low - min_move
            # 连续 3 根 1m K 没收复
            if breakdown and btc_1m is not None:
                close_1m = btc_1m['close'].values
                not_recovered = all(c < confirmed_low for c in close_1m[-3:])
                if not_recovered:
                    structure_broken = True
                    reasons.append(f"BTC跌破前低{confirmed_low:.1f}")
        
        # 2. Lower High + Lower Low → RISK_OFF
        lh_ll = self._has_lower_high() and self._has_lower_low()
        if lh_ll:
            reasons.append("LH+LL结构")
        
        # 3. DISTRIBUTION 评分
        dist_score = self._compute_distribution_score(
            btc_current, ema20_slope, breadth_smooth, btc_vol
        )
        
        # ─── 状态判定（优先级从高到低） ─────────────
        new_regime = self.current_regime
        primary = ""
        
        # 紧急状态不受最短保持时间限制
        immediate = new_regime in IMMEDIATE_REGIMES
        
        # SYSTEM_HALT / LOSS_COOLDOWN / GLOBAL_LONG_FREEZE 由外部设置
        
        if self.long_frozen:
            new_regime = MarketRegime.GLOBAL_LONG_FREEZE
            primary = "GLOBAL_LONG_FREEZE"
        
        elif structure_broken:
            # 结构破坏可以立即升级
            new_regime = MarketRegime.RISK_OFF
            primary = "STRUCTURE_BREAKDOWN"
        
        elif lh_ll:
            # LH+LL 也立即升级
            new_regime = MarketRegime.RISK_OFF
            primary = "LH_LL"
        
        elif dist_score >= 3 and can_change:
            new_regime = MarketRegime.DISTRIBUTION
            primary = f"DISTRIBUTION_SCORE={dist_score}"
        
        elif btc_current > btc_ema20 and self._has_higher_low() and can_change:
            new_regime = MarketRegime.RISK_ON
            primary = "HIGHER_LOW+EMA20"
        
        elif can_change:
            # 默认其他状态
            if breadth_smooth < 0.35:
                new_regime = MarketRegime.RISK_OFF
                primary = "BREADTH_TOO_LOW"
            elif 0.35 <= breadth_smooth < 0.45:
                new_regime = MarketRegime.DISTRIBUTION
                primary = "WEAK_BREADTH"
            elif btc_current < btc_ema20 * 0.995:
                new_regime = MarketRegime.PULLBACK
                primary = "BELOW_EMA20"
            else:
                new_regime = MarketRegime.CHOP
                primary = "CHOP"
        
        return new_regime, primary, reasons
    
    # ─── 方向许可 ──────────────────────────────────
    
    def get_direction_permission(self, snapshot: MarketSnapshot) -> dict:
        """
        纯函数：基于已生成的 snapshot 判断方向许可，不联网。
        {
            'allowed': bool,
            'direction': str,
            'long_factor': float,
            'short_factor': float,
            'primary_reason': str,
            'secondary_reasons': list[str],
            'regime': str,
        }
        """
        result = {
            'allowed': True,
            'direction': 'long',
            'long_factor': 1.0,
            'short_factor': 0.3,
            'primary_reason': '',
            'secondary_reasons': [],
            'regime': snapshot.regime.value,
            'mode': self.mode.value,
        }
        
        # 数据无效优先于其他任何判断
        if not snapshot.data_valid:
            result['allowed'] = False
            result['primary_reason'] = 'DATA_INVALID'
            return result
        
        regime = snapshot.regime
        
        if regime == MarketRegime.SYSTEM_HALT:
            result['allowed'] = False
            result['primary_reason'] = 'SYSTEM_HALT'
            return result
        
        if regime == MarketRegime.DATA_INVALID:
            result['allowed'] = False
            result['primary_reason'] = 'DATA_INVALID'
            return result
        
        if regime == MarketRegime.LOSS_COOLDOWN or snapshot.loss_cooldown:
            result['allowed'] = False
            result['primary_reason'] = 'LOSS_COOLDOWN'
            return result
        
        if self._profit_lock_active:
            result['allowed'] = False
            result['primary_reason'] = 'PROFIT_LOCK'
            return result
        
        if regime == MarketRegime.GLOBAL_LONG_FREEZE:
            result['direction'] = 'short'
            result['long_factor'] = 0.0
            result['short_factor'] = 0.5
            result['primary_reason'] = self.long_freeze_reason or 'GLOBAL_LONG_FREEZE'
            result['secondary_reasons'].append('LONG_FROZEN')
        
        elif regime == MarketRegime.RISK_OFF:
            result['direction'] = 'short'
            result['long_factor'] = 0.0
            result['short_factor'] = 0.5
            result['primary_reason'] = 'RISK_OFF'
            # 不追空保护：基于 snapshot 的 BTC 价格变化
            btc_price = snapshot.btc_state.get('price', 0)
        
        elif regime == MarketRegime.DISTRIBUTION:
            result['direction'] = 'neutral'
            result['long_factor'] = 0.2
            result['short_factor'] = 0.3
            result['primary_reason'] = 'DISTRIBUTION'
        
        elif regime == MarketRegime.PROFIT_LOCK:
            result['allowed'] = False
            result['primary_reason'] = 'PROFIT_LOCK'
        
        elif regime == MarketRegime.PULLBACK:
            result['direction'] = 'long'
            result['long_factor'] = 0.5
            result['short_factor'] = 0.3
            result['primary_reason'] = 'PULLBACK'
        
        elif regime == MarketRegime.CHOP:
            result['direction'] = 'neutral'
            result['long_factor'] = 0.3
            result['short_factor'] = 0.2
            result['primary_reason'] = 'CHOP'
        
        elif regime == MarketRegime.RISK_ON:
            result['direction'] = 'long'
            result['long_factor'] = 1.0
            result['short_factor'] = 0.3
            result['primary_reason'] = 'RISK_ON'
        
        # MONITOR 模式下只记录，不阻止
        if self.mode == Mode.MONITOR:
            result['allowed'] = True  # 只记录，不阻止
        
        return result
    
    # ─── 生成全局快照 ──────────────────────────────
    
    def _transition_to(self, new_regime, primary, reasons=None):
        '''统一状态转换（含最短保持时间检查）'''
        if new_regime == self.current_regime:
            return
        now = time.time()
        time_in_state = now - self.regime_changed_at
        min_hold = MIN_HOLD_TIME.get(self.current_regime, 0)
        can_switch = time_in_state >= min_hold or new_regime in IMMEDIATE_REGIMES
        if not can_switch:
            return
        self.prev_regime = self.current_regime
        self.current_regime = new_regime
        self.regime_changed_at = now
        logger.info(f'🔄 市场状态: {self.prev_regime.value} → {new_regime.value} | {primary}')
        self.regime_history.append({
            'time': datetime.now().isoformat(),
            'from': self.prev_regime.value,
            'to': new_regime.value,
            'reason': primary,
            'reasons': reasons or [],
        })
        if len(self.regime_history) > 100:
            self.regime_history.pop(0)

    def _refresh_top50(self) -> list:
        """按7日成交额刷新 Top50 样本池"""
        now = time.time()
        if self._top50_cache and now - self._top50_cache_time < 86400:
            return self._top50_cache
        try:
            r = req.get(f'{get_exchange_config().fapi_v1_base}/ticker/24hr',
                        proxies={"http": PROXY, "https": PROXY}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                usdt = []
                for t in data:
                    sym = t["symbol"]
                    if not sym.endswith("USDT"):
                        continue
                    base = sym[:-4]
                    if base in ("USDC","BUSD","DAI","FDUSD","TUSD"):
                        continue
                    if base.endswith(("UP","DOWN","BULL","BEAR")):
                        continue
                    usdt.append((sym, float(t.get("quoteVolume",0))))
                usdt.sort(key=lambda x: x[1], reverse=True)
                self._top50_cache = [s for s,v in usdt[:50]]
                self._top50_cache_time = now
                logger.info(f"  Top50样本池: {len(self._top50_cache)}个")
        except Exception as e:
            logger.warning(f"  Top50刷新失败: {e}")
        return self._top50_cache or []

    def _check_loss_cooldown(self) -> bool:
        """连续亏损冷却"""
        try:
            state = json.loads(open(str(BOT_STATE_FILE)).read())
            trades = state.get("trades", [])
            window = time.time() - 1800
            recent_losses = sum(1 for t in trades
                if t.get("action") == "CLOSE"
                and (t.get("pnl",0) or 0) < 0
                and _trade_time(t) > window)
            now = time.time()
            if recent_losses >= 3 and not self.loss_cooldown:
                logger.warning(f"连亏{recent_losses}笔，冷却15分钟")
                self.loss_cooldown = True
                self._loss_start_time = now
            if self.loss_cooldown and now - self._loss_start_time > 900:
                self.loss_cooldown = False
        except Exception as e:
            logger.debug(f"LOSS_COOLDOWN检查跳过: {e}")
        return self.loss_cooldown

    def _check_profit_lock(self) -> bool:
        """批量止盈锁定"""
        try:
            state = json.loads(open(str(BOT_STATE_FILE)).read())
            trades = state.get("trades", [])
            now = time.time()
            window = now - 600
            tp_count = sum(1 for t in trades
                if t.get("action") == "CLOSE"
                and "止盈" in t.get("reason","")
                and _trade_time(t) > window)
            if tp_count >= 3 and not self._profit_lock_active:
                logger.info(f"批量止盈{tp_count}笔，利润锁定60秒")
                self._profit_lock_active = True
                self._profit_lock_start = now
            if self._profit_lock_active and now - self._profit_lock_start > 900:
                self._profit_lock_active = False
        except:
            pass
        return self._profit_lock_active

    def build_snapshot(self) -> MarketSnapshot:
        """生成当前全局市场快照"""
        self._snapshot_counter += 1
        
        # 更新摆动点
        btc_5m = self._fetch_klines('BTCUSDT', '5m', limit=100)
        if btc_5m is not None:
            self._update_swing_points(btc_5m)
        
        # 计算市场宽度（每轮只计算一次）
        breadth_raw, breadth_smooth = self._calc_breadth()
        
        # 计算状态（传入宽度，避免重复计算）
        new_regime, primary, reasons = self._evaluate_regime(
            precomputed_breadth=(breadth_raw, breadth_smooth)
        )
        
        # 状态切换（含最短保持时间检查）
        if new_regime != self.current_regime:
            self._transition_to(new_regime, primary, reasons)
        
        btc_data_valid = btc_5m is not None and len(btc_5m) >= 20
        breadth_valid = breadth_raw >= 0 and breadth_smooth >= 0
        data_valid = btc_data_valid and breadth_valid
        
        self._check_profit_lock()

        snapshot = MarketSnapshot(
            version=self._snapshot_counter,
            generated_at=time.time(),
            btc_state={'price': btc_5m['close'].iloc[-1] if btc_5m is not None else 0},
            eth_state={},
            regime=self.current_regime,
            breadth_raw=breadth_raw,
            breadth_smooth=breadth_smooth,
            long_freeze=self.long_frozen,
            loss_cooldown=self._check_loss_cooldown(),
            profit_lock=self._profit_lock_active,
            long_factor=1.0,
            short_factor=1.0,
            data_valid=data_valid,
            primary_reason=primary,
            secondary_reasons=reasons,
        )
        
        perm = self.get_direction_permission(snapshot)
        snapshot.long_factor = perm['long_factor']
        snapshot.short_factor = perm['short_factor']
        
        self._last_snapshot = snapshot
        self._last_snapshot_time = time.time()
        
        return snapshot
    
    def get_snapshot(self) -> Optional[MarketSnapshot]:
        """获取缓存的快照（60 秒内有效）"""
        if self._last_snapshot and time.time() - self._last_snapshot_time < MAX_STATE_AGE:
            return self._last_snapshot
        return self.build_snapshot()
    
    def set_long_freeze(self, active: bool, reason: str = ""):
        """手动设置/解除多头冻结"""
        self.long_frozen = active
        self.long_freeze_reason = reason
        if active:
            logger.warning(f'🧊 多头冻结: {reason}')


# ═══════════════════════════════════════════════════
#  单例
# ═══════════════════════════════════════════════════

_instance = None

def get_manager(mode: Mode = None) -> MarketStateManager:
    global _instance
    """获取/创建全局市场结构管理器（单例）"""
    if mode is None:
        env_mode = os.environ.get('MARKET_STRUCTURE_MODE', 'monitor').lower()
        mode = Mode.ACTIVE if env_mode == "active" else Mode.MONITOR
    if _instance is None:
        _instance = MarketStateManager(mode=mode)
    return _instance

def build_global_market_snapshot() -> MarketSnapshot:
    """给 scalper 调用的快捷入口"""
    mgr = get_manager()
    return mgr.build_snapshot()

def check_trade_permission(symbol: str, side: str) -> dict:
    """
    超短线开单前调用的最终检查。
    返回 {'policy_allowed': True/False, 'reason': str, 'risk_factor': float}
    """
    mgr = get_manager()
    snap = mgr.get_snapshot()
    if not snap:
        return {'policy_allowed': False, 'reason': 'NO_SNAPSHOT', 'risk_factor': 0}
    
    perm = mgr.get_direction_permission(snap)
    
    # 数据无效时禁止新开仓
    if not snap.data_valid:
        if snap.breadth_raw <= 0 or snap.breadth_smooth <= 0:
            return {"policy_allowed": False, "effective_allowed": False, "reason": "BREADTH_INVALID", "risk_factor": 0}
        return {'policy_allowed': False, 'reason': 'DATA_INVALID', 'risk_factor': 0}
    
    # 方向匹配检查
    direction = 'long' if side == 'LONG' else 'short'
    if perm.get('policy_allowed', False) is False and mgr.mode == Mode.ACTIVE:
        return {'policy_allowed': False, 'reason': f"BLOCKED_{perm['primary_reason']}", 'risk_factor': 0}
    # 方向匹配检查
    if perm['direction'] == 'long' and direction == 'short':
        if perm['short_factor'] <= 0:
            return {'policy_allowed': False, 'reason': f"SHORT_NOT_ALLOWED_{perm['primary_reason']}",
                    'risk_factor': 0}
        return {'policy_allowed': True, 'reason': f"SHORT_OK_{perm['primary_reason']}",
                'risk_factor': perm['short_factor']}
    elif perm['direction'] == 'short' and direction == 'long':
        if perm['long_factor'] <= 0:
            return {'policy_allowed': False, 'reason': f"LONG_FROZEN_{perm['primary_reason']}",
                    'risk_factor': 0}
        return {'policy_allowed': True, 'reason': f"LONG_OK_{perm['primary_reason']}",
                'risk_factor': perm['long_factor']}
    elif perm['direction'] == 'neutral':
        risk = perm['long_factor'] if direction == 'long' else perm['short_factor']
        if risk <= 0:
            return {'policy_allowed': False, 'reason': f"NEUTRAL_NO_{perm['primary_reason']}",
                    'risk_factor': 0}
        return {'policy_allowed': True, 'reason': f"NEUTRAL_OK_{perm['primary_reason']}",
                'risk_factor': risk}
    
    # 方向上一致
    if not perm['allowed']:
        return {'policy_allowed': False, 'reason': perm['primary_reason'], 'risk_factor': 0}
    
    risk = perm['long_factor'] if direction == 'long' else perm['short_factor']
    return {'policy_allowed': True, 'reason': perm['primary_reason'], 'risk_factor': risk}


# ═══════════════════════════════════════════════════
#  命令行测试
# ═══════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
    
    mgr = get_manager(mode=Mode.MONITOR)
    snap = mgr.build_snapshot()
    perm = mgr.get_direction_permission(snap)
    
    print(f'\n{"="*50}')
    print(f'市场状态: {snap.regime.value}')
    print(f'市场宽度: raw={snap.breadth_raw:.1%} smooth={snap.breadth_smooth:.1%}')
    print(f'多方向系数: {snap.long_factor:.2f}')
    print(f'空方向系数: {snap.short_factor:.2f}')
    print(f'主因: {snap.primary_reason}')
    print(f'次因: {snap.secondary_reasons}')
    print(f'方向许可: {perm["direction"]}')
    print(f'允许交易: {perm["allowed"]}')
    print(f'快照版本: {snap.version}')
    print(f'{"="*50}')

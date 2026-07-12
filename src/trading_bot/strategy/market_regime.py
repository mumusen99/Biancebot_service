"""
市场环境判断 + 币种分层筛选
===========================
第1层：BTC 环境 → 决定多空方向
第2层：山寨筛选 → Relative Strength + 成交量 + 均线偏离
"""
import json
import logging
import time
import hmac
import hashlib
import urllib.parse
from typing import Optional
from pathlib import Path

import requests as req
import pandas as pd
import numpy as np

from trading_bot.core.settings import API_KEY, API_SECRET, PROXY
from trading_bot.exchange.market_data import fetch_klines, fetch_ticker

logger = logging.getLogger("market_regime")

from trading_bot.core.env_config import get_exchange_config

try:
    FAPI_BASE = get_exchange_config().fapi_v1_base
except Exception:
    FAPI_BASE = "https://fapi.binance.com/fapi/v1"
_session = req.Session()
_session.proxies = {"http": PROXY, "https": PROXY}


# ═══════════════════════════════════════════════════
#  第1层：BTC 环境判断
# ═══════════════════════════════════════════════════

from trading_bot.exchange.gateway import get_gateway
_gw = get_gateway()

def _signed_get(path: str, params: dict = None) -> list:
    """签名 GET — 委托给 ExchangeGateway。"""
    try:
        rid = _gw._request_id()
        return _gw._call("GET", FAPI_BASE, path, params or {}, rid, "")
    except Exception as e:
        raise Exception(f"GET {path} failed: {e}") from e


def get_btc_environment(timeframe: str = "1h") -> dict:
    """
    判断 BTC 当前环境（滑动评分制，非二元禁多/禁空）。
    
    返回:
      regime: str        'strong_bull' / 'bull' / 'mild_bull' / 'range' / 'mild_bear' / 'bear' / 'strong_bear'
      bias: int          -10(极空) ~ +10(极多), 0=中性
      direction: str     'long' / 'both' / 'short' (只在极端时才hard block)
      detail: str
    """
    try:
        df = fetch_klines(None, "BTCUSDT", timeframe=timeframe, limit=100)
        if df is None or df.empty or len(df) < 50:
            return {"regime": "unknown", "bias": 0, "direction": "both",
                    "rsi": 50, "change_24h": 0, "detail": "数据不足"}
        
        close = df["close"]
        last = close.iloc[-1]
        
        # 技术指标
        sma20 = close.rolling(20).mean().iloc[-1]
        sma50 = close.rolling(50).mean().iloc[-1]
        
        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        rsi = (100 - (100 / (1 + rs))).iloc[-1]
        
        # 价格位置
        price_above_sma20 = last > sma20
        price_above_sma50 = last > sma50
        sma_bullish = sma20 > sma50  # 多头排列
        
        # 回调深度 (%)
        recent_high = close.tail(20).max()
        pullback_pct = (recent_high - last) / recent_high * 100
        
        # 24h 变化
        change_24h = _get_24h_change("BTCUSDT")
        
        # ─── 滑动评分：每个因子贡献 ±分 ───
        score = 0
        
        # RSI 贡献 (±3)
        if rsi > 60:
            score += 3
        elif rsi > 55:
            score += 2
        elif rsi > 50:
            score += 1
        elif rsi < 35:
            score -= 3
        elif rsi < 40:
            score -= 2
        elif rsi < 45:
            score -= 1
        
        # 均线位置贡献 (±2)
        if price_above_sma20 and price_above_sma50:
            score += 2
        elif price_above_sma20:
            score += 1
        elif not price_above_sma20 and not price_above_sma50:
            score -= 2
        else:
            score -= 1
        
        # 均线排列贡献 (±2)
        if sma_bullish:
            score += 2
        else:
            score -= 2
        
        # 回调深度贡献 (±1)
        if pullback_pct < 2:
            score += 1  # 浅回调
        elif pullback_pct > 8:
            score -= 1  # 深回调
        
        # 24h 变化贡献 (±2)
        if change_24h > 1:
            score += 2
        elif change_24h > 0.3:
            score += 1
        elif change_24h < -2:
            score -= 2
        elif change_24h < -0.5:
            score -= 1
        
        # 总分范围 -10 ~ +10
        score = max(-10, min(10, score))
        
        # ─── 根据评分定档 ───
        if score >= 7:
            regime = "strong_bull"
            direction = "long"
        elif score >= 4:
            regime = "bull"
            direction = "long"
        elif score >= 1:
            regime = "mild_bull"
            direction = "both"  # 偏多但不禁止做空
        elif score >= -2:
            regime = "range"
            direction = "both"
        elif score >= -5:
            regime = "mild_bear"
            direction = "both"  # 偏空但不禁止做多
        elif score >= -7:
            regime = "bear"
            direction = "short"
        else:
            regime = "strong_bear"
            direction = "short"
        
        detail = (f"价格{last:.0f} RSI{rsi:.0f} MA20{sma20:.0f} MA50{sma50:.0f} "
                  f"回调{pullback_pct:.1f}% 24h{change_24h:+.2f}% 评分{score:+.0f}")
        
        return {
            "regime": regime,
            "bias": score,
            "direction": direction,
            "rsi": round(rsi, 1),
            "change_24h": round(change_24h, 2),
            "pullback_pct": round(pullback_pct, 1),
            "detail": detail,
        }
        
    except Exception as e:
        logger.warning(f"BTC环境判断失败: {e}")
        return {"regime": "unknown", "bias": 0, "direction": "both",
                "rsi": 50, "change_24h": 0, "detail": str(e)}


def _get_24h_change(symbol: str) -> float:
    """获取24h涨跌幅"""
    try:
        ticker = fetch_ticker(None, symbol)
        if ticker:
            return ticker.get("change24h", 0) or 0
    except Exception:
        pass
    return 0


# ═══════════════════════════════════════════════════
#  第2层：山寨筛选评分
# ═══════════════════════════════════════════════════

def score_coin(symbol: str, btc_regime: dict) -> Optional[dict]:
    """
    对单个币打分，返回评分结果。
    评分维度：
      - relative_strength: 相对BTC强度（最重要）
      - volume_surge: 成交量放大倍数
      - ma_deviation: 均线偏离度（追高惩罚）
      - overall: 综合分
    """
    try:
        # 24h 数据
        ticker = fetch_ticker(None, symbol)
        if not ticker:
            return None
        
        cur_price = ticker.get("last", 0)
        change_24h = ticker.get("change24h", 0) or 0
        volume = ticker.get("volume24h", 0) or 0
        quote_vol = ticker.get("quoteVolume", 0) or 0
        high_24h = ticker.get("high24h", 0) or 0
        low_24h = ticker.get("low24h", 0) or 0
        
        btc_change = btc_regime.get("change_24h", 0)
        
        # 相对强度 = 币涨跌幅 - BTC涨跌幅
        relative_strength = change_24h - btc_change
        
        # K线分析（均线位置 + 成交量对比）
        df = fetch_klines(None, symbol, timeframe="1h", limit=48)
        if df is None or df.empty or len(df) < 24:
            # 数据不足时降级只用24h数据
            sma20 = cur_price
            vol_avg = 1
            vol_ratio = 1
        else:
            close_series = df["close"]
            sma20 = close_series.tail(20).mean()
            vol_series = df["volume"]
            vol_avg = vol_series.tail(24).mean()
            vol_current = vol_series.iloc[-1]
            vol_ratio = vol_current / vol_avg if vol_avg > 0 else 1
        
        # 均线偏离度（正值 = 价格在均线上方）
        ma_deviation = (cur_price - sma20) / sma20 * 100 if sma20 > 0 else 0
        
        # 24h振幅
        amplitude = (high_24h - low_24h) / low_24h * 100 if low_24h > 0 else 0
        
        # ─── 综合评分 ───
        bias = btc_regime.get("bias", 0)
        
        # 相对强度分（降低24h权重，±5）
        rs_score = max(-5, min(5, relative_strength * 1.0))
        
        # 成交量分（0~5）
        vol_score = min(5, max(0, (vol_ratio - 1) * 5)) if vol_ratio > 0 else 0
        
        # 均线偏离惩罚（追高>5%扣分，深度回调加分）
        ma_score = 0
        if ma_deviation > 8:
            ma_score = -5
        elif ma_deviation < -8:
            ma_score = -3
        elif -3 <= ma_deviation <= 3:
            ma_score = 3
        elif -5 <= ma_deviation <= 5:
            ma_score = 1
        
        # 振幅加分
        amp_score = min(2, max(0, amplitude / 5)) if 3 < amplitude < 30 else 0

        # 衰竭惩罚：24h极强但当前价格低于24h高点较多 → 动能衰竭
        exhaustion_penalty = 0
        if high_24h > 0 and change_24h > 5:
            pct_from_high = (high_24h - cur_price) / high_24h * 100
            if pct_from_high > 3:
                exhaustion_penalty = -1.5  # 从高点回落超3%，已衰竭
        
        # 综合
        overall = rs_score + vol_score + ma_score + amp_score + exhaustion_penalty
        
        return {
            "symbol": symbol,
            "price": cur_price,
            "change_24h": round(change_24h, 2),
            "relative_strength": round(relative_strength, 2),
            "volume_ratio": round(vol_ratio, 1),
            "ma_deviation": round(ma_deviation, 2),
            "overall": round(overall, 1),
        }
        
    except Exception as e:
        return None


# ═══════════════════════════════════════════════════
#  全市场扫描（供 scalper / auto_trader 调用）
# ═══════════════════════════════════════════════════

def scan_top_coins(
    min_volume_usdt: float = 500000,
    max_coins: int = 100,
    top_n: int = 20,
    only_long: bool = False,
    only_short: bool = False,
) -> list:
    """
    全市场扫描，返回评分最高的 top_n 个币。
    
    BTC 环境决定方向偏好：
      bull → 优先评分高的做多标的
      bear → 评分高的做空标的（如果支持）
      range → 多空都看
    """
    btc_env = get_btc_environment()
    logger.info(f"BTC环境: {btc_env['regime']} ({btc_env['detail']}) → 方向: {btc_env['direction']}")
    
    # 获取所有 USDT 合约
    tickers = _signed_get("ticker/24hr")
    
    # 按交易量排序筛选
    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym == "BTCUSDT":
            continue
        qv = float(t.get("quoteVolume", 0))
        if qv < min_volume_usdt:
            continue
        candidates.append((sym, qv))
    
    candidates.sort(key=lambda x: x[1], reverse=True)
    top_by_volume = [sym for sym, _ in candidates[:max_coins]]
    
    logger.info(f"待筛选: {len(top_by_volume)}个（Top{max_coins}成交量）")
    
    # 代理节点并发差，少量并发 + 重试兜底
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(score_coin, sym, btc_env): sym for sym in top_by_volume}
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)
    
    # 按综合评分排序
    results.sort(key=lambda x: x["overall"], reverse=True)
    
    # 根据BTC环境调整筛选（滑动评分制）
    regime = btc_env.get("regime", "unknown")
    direction = btc_env.get("direction", "both")
    bias = btc_env.get("bias", 0)
    
    # 只有极端方向才硬性过滤
    if direction == "long":
        # strong_bull / bull: 过滤还在大跌的币
        results = [r for r in results if r["change_24h"] > -4]
    elif direction == "short":
        # strong_bear / bear: 注意涨太多的币可能补跌
        results = [r for r in results if r["change_24h"] < 5]
    
    # 非极端时仅凭评分排序，不做硬过滤
    # bias 值作为提示传给调用方，由策略自行决定
    
    top = results[:top_n]
    logger.info(f"Top{top_n} 最优: {top[0]['symbol'] if top else '无'} (评分{top[0]['overall'] if top else 0})")
    
    return top, btc_env


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    top, env = scan_top_coins(top_n=10)
    print(f"\nBTC环境: {env['regime']} RSI{env.get('rsi','?')} 方向={env['direction']}")
    print(f"{'币种':<14} {'24h%':>7} {'相对强度':>8} {'量比':>5} {'均线偏离':>8} {'评分':>5}")
    print("-" * 55)
    for r in top:
        print(f"{r['symbol']:<14} {r['change_24h']:>+6.2f}% {r['relative_strength']:>+7.2f} {r['volume_ratio']:>4.1f}x {r['ma_deviation']:>+7.2f}% {r['overall']:>+5.1f}")

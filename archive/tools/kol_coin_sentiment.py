#!/usr/bin/env python3
"""
针对性币种 KOL 情绪分析
=====================
对每个候选币种，从网络上检索最新相关的 KOL/新闻观点，
分析针对性情绪，替代原来只看大盘面新闻的方式。

用法:
  python3 -c "
    from kol_coin_sentiment import get_coin_sentiment
    result = get_coin_sentiment(['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
    print(result)
  "
"""

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests as req

from config import PROXY

logger = logging.getLogger("kol_coin")

BASE_DIR = Path(__file__).parent
SENTIMENT_FILE = BASE_DIR / "sentiment.json"

# ─── 币种名映射（symbol → 可读名称） ──────────────────
COIN_NAMES = {
    "BTCUSDT": "Bitcoin BTC", "ETHUSDT": "Ethereum ETH",
    "BNBUSDT": "BNB BNB Chain", "SOLUSDT": "Solana SOL",
    "XRPUSDT": "XRP Ripple", "DOGEUSDT": "Dogecoin DOGE",
    "ADAUSDT": "Cardano ADA", "AVAXUSDT": "Avalanche AVAX",
    "LINKUSDT": "Chainlink LINK", "DOTUSDT": "Polkadot DOT",
    "MATICUSDT": "Polygon MATIC", "NEARUSDT": "Near Protocol NEAR",
    "APTUSDT": "Aptos APT", "ARBUSDT": "Arbitrum ARB",
    "OPUSDT": "Optimism OP", "PEPEUSDT": "Pepe PEPE",
    "XAUUSDT": "Gold XAU", "XAGUSDT": "Silver XAG",
    "SUIUSDT": "Sui SUI", "MUUSDT": "Mumu MU",
    "OGNUSDT": "Origin OGN", "WLDUSDT": "Worldcoin WLD",
    "BCHUSDT": "Bitcoin Cash BCH", "1000PEPEUSDT": "Pepe PEPE",
    "ZECUSDT": "Zcash ZEC", "SPCXUSDT": "SpaceX SPCX",
}

# ─── 关键词评分 ──────────────────────────────────────
BULLISH_KW = {
    "bullish": 2, "buy": 2, "long": 2, "rally": 3, "breakout": 3,
    "surge": 2, "moon": 2, "pump": 2, "accumulation": 2, "support": 1,
    "upgrade": 2, "partnership": 2, "adoption": 2, "etf": 3, "halving": 2,
    "upgrade": 2, "launch": 1, "positive": 1, "growth": 1, "gain": 1,
    "看多": 3, "买入": 2, "抄底": 3, "反弹": 2, "利好": 3, "突破": 3,
    "涨": 1, "牛市": 3, "做多": 3, "牛回": 3, "起飞": 2,
    "up only": 3, "bottom": 1, "oversold": 2, "strong": 1,
}

BEARISH_KW = {
    "bearish": 2, "sell": 2, "short": 2, "crash": 3, "dump": 3,
    "liquidation": 2, "correction": 2, "resistance": 1, "ban": 3,
    "regulation": 2, "hack": 3, "fraud": 3, "fud": 2,
    "sell-off": 3, "decline": 1, "loss": 1, "risk": 1,
    "看空": 3, "卖出": 2, "逃顶": 3, "回调": 2, "利空": 3, "崩盘": 3,
    "暴跌": 3, "跌": 1, "熊市": 3, "做空": 3, "监管": 2, "禁令": 3,
    "割": 2, "跑路": 3, "归零": 3,
    "resist": 1, "overbought": 2, "weak": 1, "drop": 1,
}

RISK_KW = ["market crash", "liquidation cascade", "exchange hack",
           "protocol exploit", "破产", "崩盘", "监管打击", "全面禁止",
           "delist", "scam", "rug pull"]


def _search_google_news(query: str, max_items: int = 10) -> list:
    """通过 Google News RSS 搜索特定币种的新闻"""
    results = []
    try:
        url = f"https://news.google.com/rss/search?q={query.replace(' ','+')}+crypto&hl=en-US&gl=US&ceid=US:en"
        prox = {"http": PROXY, "https": PROXY}
        r = req.get(url, proxies=prox, timeout=10,
                    headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return results
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            title = item.find("title")
            if title is not None and title.text:
                results.append(title.text)
            if len(results) >= max_items:
                break
    except Exception as e:
        logger.debug(f"Google News search failed: {e}")
    return results


def _search_crypto_sites(query: str, max_items: int = 5) -> list:
    """从加密媒体站点搜索特定币种"""
    results = []
    prox = {"http": PROXY, "https": PROXY}
    headers = {"User-Agent": "Mozilla/5.0"}
    
    rss_sources = [
        ("CoinTelegraph", f"https://cointelegraph.com/rss/tag/{query.lower().split()[0]}"),
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ]
    
    for name, url in rss_sources:
        try:
            r = req.get(url, proxies=prox, timeout=10, headers=headers)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            count = 0
            for item in root.iter("item"):
                title = item.find("title")
                if title is not None and title.text:
                    # 只保留提到该币种的文章
                    coin_keywords = query.lower().split()[:2]
                    title_lower = title.text.lower()
                    if any(kw in title_lower for kw in coin_keywords):
                        results.append(title.text)
                        count += 1
                if count >= max_items:
                    break
        except Exception as e:
            logger.debug(f"{name} search failed: {e}")
    
    return results


def _search_crypto_panic(query: str, max_items: int = 5) -> list:
    """搜索 CryptoPanic 聚合新闻 (不用 API key 的公开页面)"""
    results = []
    try:
        prox = {"http": PROXY, "https": PROXY}
        coin_short = query.split()[0]
        url = f"https://cryptopanic.com/news/{coin_short.lower()}/"
        r = req.get(url, proxies=prox, timeout=10,
                    headers={"User-Agent": "Mozilla/5.0",
                             "Accept": "text/html,application/xhtml+xml"})
        if r.status_code != 200:
            return results
        # 简单爬取标题
        titles = re.findall(r'<a[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</a>',
                           r.text, re.IGNORECASE | re.DOTALL)
        for t in titles:
            clean = re.sub(r'<[^>]+>', '', t).strip()
            if clean and len(clean) > 10:
                results.append(clean)
            if len(results) >= max_items:
                break
    except Exception as e:
        logger.debug(f"CryptoPanic search failed: {e}")
    return results


def _analyze_sentiment(headlines: list) -> dict:
    """对一组标题进行关键词情绪分析"""
    bull_score = 0
    bear_score = 0
    risk = False
    signals = []
    
    for h in headlines:
        title_lower = h.lower()
        b_score = 0
        s_score = 0
        
        for kw, w in BULLISH_KW.items():
            if kw in title_lower:
                b_score += w
        for kw, w in BEARISH_KW.items():
            if kw in title_lower:
                s_score += w
        for kw in RISK_KW:
            if kw in title_lower:
                risk = True
                break
        
        bull_score += b_score
        bear_score += s_score
        
        if b_score > s_score * 1.3:
            signals.append("bullish")
        elif s_score > b_score * 1.3:
            signals.append("bearish")
        else:
            signals.append("neutral")
    
    total = bull_score + bear_score
    if total == 0:
        return {"sentiment": "neutral", "score": 0,
                "detail": "no data", "risk": False}
    
    # 情绪判定 (加权)
    ratio = (bull_score - bear_score) / total * 100  # -100 ~ +100
    
    if ratio > 15:
        sentiment = "bullish"
    elif ratio < -15:
        sentiment = "bearish"
    else:
        sentiment = "neutral"
    
    bull_articles = signals.count("bullish")
    bear_articles = signals.count("bearish")
    
    detail = f"bull{bull_articles}/bear{bear_articles} ({bull_score}/{bear_score}pts) sent={sentiment}"
    
    return {
        "sentiment": sentiment,
        "score": round(ratio, 1),
        "detail": detail,
        "risk": risk,
        "bull_articles": bull_articles,
        "bear_articles": bear_articles,
        "total_articles": len(headlines),
        "bull_score": bull_score,
        "bear_score": bear_score,
    }


def get_coin_sentiment(symbols: list) -> dict:
    """
    获取指定币种的针对性 KOL 情绪。
    对每个币种搜索并分析最新的网络观点。
    
    返回: { "BTCUSDT": {...}, "ETHUSDT": {...}, ... }
    """
    result = {}
    
    for sym in symbols:
        coin_name = COIN_NAMES.get(sym, sym.replace("USDT", ""))
        logger.info(f"  🔍 检索 {sym} ({coin_name}) KOL情绪...")
        
        # 1. Google News (主要来源,快速)
        news = _search_google_news(coin_name, 6)
        
        # 2. 加密专业站点 (针对补充)
        crypto_news = _search_crypto_sites(coin_name, 3)
        
        # 合并去重
        all_articles = list(dict.fromkeys(news + crypto_news))
        
        if not all_articles:
            logger.info(f"    {sym}: 未找到相关文章，标记为 neutral")
            result[sym] = {
                "sentiment": "neutral",
                "score": 0,
                "articles": [],
            }
            continue
        
        analysis = _analyze_sentiment(all_articles)
        analysis["articles"] = all_articles[:5]  # 保留前5条标题
        
        sent_icon = "🟢" if analysis["sentiment"] == "bullish" else "🔴" if analysis["sentiment"] == "bearish" else "⚪"
        logger.info(f"    {sym}: {sent_icon} {analysis['sentiment']} ({analysis['detail']})")
        
        result[sym] = analysis
        time.sleep(0.3)  # 礼貌性延迟，防止被限速
    
    return result


def update_sentiment_file(coin_sentiment: dict):
    """将币种情绪合并写入 sentiment.json"""
    try:
        if SENTIMENT_FILE.exists():
            existing = json.loads(SENTIMENT_FILE.read_text())
        else:
            existing = {}
        
        existing["last_update"] = datetime.now().isoformat()
        existing["coin_sentiment"] = coin_sentiment
        
        # 更新总览（从各币取多数）
        sentiments = [v["sentiment"] for v in coin_sentiment.values()]
        bulls = sentiments.count("bullish")
        bears = sentiments.count("bearish")
        neutrals = sentiments.count("neutral")
        
        if bulls > bears + neutrals:
            overall = "bullish"
        elif bears > bulls + neutrals:
            overall = "bearish"
        else:
            overall = "neutral"
        
        risk = any(v.get("risk", False) for v in coin_sentiment.values())
        
        total = bulls + bears + neutrals
        existing["overall_sentiment"] = overall
        existing["details"] = {
            "bullish_count": bulls,
            "bearish_count": bears,
            "neutral_count": neutrals,
            "bullish_pct": round(bulls / max(total, 1) * 100, 1),
            "bearish_pct": round(bears / max(total, 1) * 100, 1),
        }
        existing["risk_warning"] = risk
        existing["analysis_text"] = f"币种KOL: 多{bulls}/空{bears}/中{neutrals}, 整体{overall}"
        
        SENTIMENT_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
        logger.info(f"✅ 币种KOL情感已写入: 多{bulls}/空{bears}")
        
        return overall
    except Exception as e:
        logger.error(f"写入sentiment.json失败: {e}")
        return "neutral"


def run_for_symbols(symbols: list = None):
    """
    对指定币种运行 KOL 针对性分析并更新文件。
    symbols: 如果不传，默认分析配置中的币种
    """
    from config import SYMBOLS
    targets = symbols or SYMBOLS
    
    if not targets:
        logger.warning("没有需要分析的币种")
        return
    
    logger.info(f"🔬 开始针对性KOL分析: {len(targets)}个币种")
    coin_sent = get_coin_sentiment(targets)
    overall = update_sentiment_file(coin_sent)
    
    # 汇总输出
    arrow = "🟢" if overall == "bullish" else "🔴" if overall == "bearish" else "⚪"
    print(f"{arrow} KOL: {overall} | 币种分析: {len(coin_sent)}个")
    for sym, data in sorted(coin_sent.items(), key=lambda x: abs(x[1].get("score", 0)), reverse=True)[:10]:
        icon = "🟢" if data["sentiment"] == "bullish" else "🔴" if data["sentiment"] == "bearish" else "⚪"
        print(f"  {sym:12s} {icon} {data['sentiment']:8s} score={data.get('score',0):+5.1f}  {data.get('detail','')}")
    
    return coin_sent


# ─── 向下兼容自动新闻系统 ────────────────────────────
def run():
    """兼容旧的 auto_news 入口，分析配置中所有币种"""
    run_for_symbols()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [kol_coin] %(message)s")
    run()

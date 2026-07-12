#!/usr/bin/env python3
"""
自动新闻情绪分析引擎 v2
===================
用 RSS 直拉新闻 → 关键词情绪打分 → 写 sentiment.json
全自动，不依赖 AI 手搜。
"""
import json
import logging
import xml.etree.ElementTree as ET
import re
from datetime import datetime
from pathlib import Path

import requests as req

from config import PROXY

logger = logging.getLogger("auto_news")

BASE = Path(__file__).parent
SENTIMENT_FILE = BASE / "sentiment.json"
FLAG_FILE = BASE / ".kol_check_due"

BULLISH_KW = {
    "bullish": 2, "buy": 2, "long": 2, "rally": 3, "breakout": 3,
    "surge": 2, "moon": 2, "pump": 2, "accumulation": 2, "support": 1,
    "upgrade": 2, "partnership": 2, "adoption": 2, "etf": 3, "halving": 2,
    "看多": 3, "买入": 2, "抄底": 3, "反弹": 2, "利好": 3, "突破": 3,
    "涨": 1, "牛市": 3, "做多": 3, "牛回": 3, "起飞": 2,
}

BEARISH_KW = {
    "bearish": 2, "sell": 2, "short": 2, "crash": 3, "dump": 3,
    "liquidation": 2, "correction": 2, "resistance": 1, "ban": 3,
    "regulation": 2, "hack": 3, "fraud": 3, "fud": 2,
    "看空": 3, "卖出": 2, "逃顶": 3, "回调": 2, "利空": 3, "崩盘": 3,
    "暴跌": 3, "跌": 1, "熊市": 3, "做空": 3, "监管": 2, "禁令": 3,
    "割": 2, "跑路": 3, "归零": 3,
}

RISK_KW = ["market crash", "liquidation cascade", "exchange hack", "protocol exploit", "破产", "崩盘", "监管打击", "全面禁止"]


def fetch_headlines() -> list:
    """从多个 RSS 源抓取最新头条"""
    headlines = []
    rss_urls = [
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("CoinTelegraph", "https://cointelegraph.com/rss"),
    ]
    
    for name, url in rss_urls:
        try:
            r = req.get(url, timeout=10, proxies={"http": PROXY, "https": PROXY})
            if r.status_code != 200:
                continue
            try:
                root = ET.fromstring(r.content)
                # RSS 2.0
                for item in root.iter("item"):
                    title_el = item.find("title")
                    if title_el is not None and title_el.text:
                        headlines.append({"title": title_el.text, "source": name})
                # Atom
                for item in root.iter("{http://www.w3.org/2005/Atom}entry"):
                    title_el = item.find("{http://www.w3.org/2005/Atom}title")
                    if title_el is not None and title_el.text:
                        headlines.append({"title": title_el.text, "source": name})
            except:
                # HTML fallback
                titles = re.findall(r'<title[^>]*>(.*?)</title>', r.text, re.DOTALL)[:15]
                for t in titles:
                    clean = re.sub(r'<[^>]+>', '', t).strip()
                    if clean and len(clean) > 10 and "404" not in clean:
                        headlines.append({"title": clean, "source": name})
        except Exception as e:
            logger.debug(f"{name} 失败: {e}")
    
    # 去重
    seen = set()
    unique = []
    for h in headlines:
        key = h["title"].lower().strip()[:30]
        if key not in seen:
            seen.add(key)
            unique.append(h)
    return unique


def analyze(headlines: list) -> dict:
    """关键词情绪打分"""
    bulls = bears = 0
    bull_score = bear_score = 0
    risk = False
    topics = set()
    
    for h in headlines:
        title = h["title"].lower()
        bs = bears_s = 0
        
        for kw, w in BULLISH_KW.items():
            if kw in title:
                bs += w
                if len(kw) > 2: topics.add(kw.capitalize())
        for kw, w in BEARISH_KW.items():
            if kw in title:
                bears_s += w
                if len(kw) > 2: topics.add(kw.capitalize())
        for kw in RISK_KW:
            if kw in title:
                risk = True
                break  # 一次触发就够了
    # 需要至少2条不同新闻触发风险才标记
    if risk:
        risk_count = sum(1 for h in headlines if any(kw in h['title'].lower() for kw in RISK_KW))
        risk = risk_count >= 2
        
        if bs > bears_s: bulls += 1; bull_score += bs
        elif bears_s > bs: bears += 1; bear_score += bears_s
    
    total = bulls + bears
    if total == 0:
        return {"overall": "neutral", "bulls": 0, "bears": 0, "risk": False, "topics": []}
    
    bull_pct = round(bulls / total * 100, 1)
    bear_pct = round(bears / total * 100, 1)
    
    if bull_pct > bear_pct + 20: overall = "bullish"
    elif bear_pct > bull_pct + 20: overall = "bearish"
    else: overall = "neutral"
    
    return {"overall": overall, "bulls": bulls, "bears": bears, "bull_pct": bull_pct, "bear_pct": bear_pct, "risk": risk, "topics": sorted(topics, key=len, reverse=True)[:8]}


def run():
    headlines = fetch_headlines()
    logger.info(f"📡 抓取 {len(headlines)} 条新闻")
    
    if len(headlines) < 3:
        logger.warning("⚠️ 新闻不足3条，保留上次结果")
        if FLAG_FILE.exists():
            FLAG_FILE.unlink()  # 清标志下次重试
        return
    
    result = analyze(headlines)
    
    sentiment = {
        "last_update": datetime.now().isoformat(),
        "overall_sentiment": result["overall"],
        "details": {
            "bullish_count": result["bulls"],
            "bearish_count": result["bears"],
            "neutral_count": len(headlines) - result["bulls"] - result["bears"],
            "bullish_pct": result["bull_pct"],
            "bearish_pct": result["bear_pct"],
        },
        "key_topics": result["topics"],
        "risk_warning": result["risk"],
        "analysis_text": f"自动新闻分析: 看多{result['bulls']}/看空{result['bears']}, 情绪{result['overall']}",
    }
    
    SENTIMENT_FILE.write_text(json.dumps(sentiment, indent=2))
    if FLAG_FILE.exists():
        FLAG_FILE.unlink()
    
    arrow = "🟢" if result["overall"] == "bullish" else "🔴" if result["overall"] == "bearish" else "⚪"
    print(f"{arrow} KOL: {result['overall']} | 多{result['bulls']}/空{result['bears']} ({result['bull_pct']}%/{result['bear_pct']}%) | 风险{'⚠️' if result['risk'] else '✅'} | {', '.join(result['topics'][:4])}")
    logger.info(f"✅ 自动KOL完成: {result['overall']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    run()

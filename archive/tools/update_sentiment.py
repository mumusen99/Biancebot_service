#!/usr/bin/env python3
"""
AI 执行 KOL 情绪分析 — 更新 sentiment.json
============================================
用法: AI运行此脚本更新情绪数据，auto_trader决策时读取
"""
import json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
SENTIMENT_FILE = BASE / "sentiment.json"
FLAG_FILE = BASE / ".kol_check_due"


def save_sentiment(data: dict):
    """保存 KOL 情绪分析结果"""
    data["last_update"] = datetime.now().isoformat()
    SENTIMENT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    # 清除标志
    if FLAG_FILE.exists():
        FLAG_FILE.unlink()
    print(f"✅ 情绪分析已保存到 sentiment.json")


def load_current() -> dict:
    """加载当前情绪数据"""
    try:
        return json.loads(SENTIMENT_FILE.read_text()) if SENTIMENT_FILE.exists() else {}
    except:
        return {}


def print_status():
    """打印当前情绪状态"""
    try:
        d = json.loads(SENTIMENT_FILE.read_text()) if SENTIMENT_FILE.exists() else {}
        print(f"📊 当前 KOL 情绪:")
        print(f"   上次更新: {d.get('last_update', '从未')}")
        print(f"   总体倾向: {d.get('overall_sentiment', '未知')}")
        print(f"   看多/看空/中性: {d.get('details',{}).get('bullish_count',0)}/{d.get('details',{}).get('bearish_count',0)}/{d.get('details',{}).get('neutral_count',0)}")
        print(f"   风险警告: {'⚠️ 是' if d.get('risk_warning') else '✅ 否'}")
        if d.get("key_topics"):
            print(f"   热点话题: {', '.join(d['key_topics'][:5])}")
    except:
        print("❌ 无法读取 sentiment.json")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        print_status()
    else:
        print("这个脚本由 AI 手动调用，通过 web_search 分析后更新 sentiment.json")
        print("用法: python3 update_sentiment.py --status")
        print()
        print("AI 执行步骤:")
        print("  1. web_search 搜币圈博主动态")
        print("  2. 解析多空情绪倾向")
        print("  3. 运行 python3 -c \"...\" 更新 sentiment.json")

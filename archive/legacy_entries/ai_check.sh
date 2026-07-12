#!/bin/bash
# ─────────────────────────────────────────────────────
# AI 市场检查脚本 — 我通过此脚本定时检查行情
# ─────────────────────────────────────────────────────
# 用法: bash ai_check.sh

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$BASE_DIR/../binance-venv"
PYTHON="$VENV_DIR/bin/python3"

# 解析 .env（避免 source 不可用的问题）
if [ -f "$BASE_DIR/.env" ]; then
  while IFS='=' read -r key val || [ -n "$key" ]; do
    key="$(echo "$key" | tr -d ' \t\n\r')"
    val="$(echo "$val" | tr -d ' \t\n\r')"
    [ -z "$key" ] || [ "${key#\#}" != "$key" ] && continue
    case "$key" in
      BINANCE_API_KEY|BINANCE_API_SECRET|BINANCE_TESTNET)
        export "$key=$val"
        ;;
    esac
  done < "$BASE_DIR/.env"
fi

echo "🔍 分析市场..."
export BINANCE_API_KEY BINANCE_API_SECRET BINANCE_TESTNET
$PYTHON "$BASE_DIR/run_check.py" --analyze 2>/dev/null

# 显示摘要
$PYTHON << 'PYEOF'
import json
base = "/vol2/@apphome/trim.openclaw/data/workspace/binance-bot"
try:
    with open(f"{base}/analysis.json") as f:
        r = json.load(f)
    s = r.get("summary", {})
    print()
    print("=" * 58)
    print(f"  时间    : {s.get('time', '?')}")
    print(f"  方向    : {s.get('direction', '?')}")
    print(f"  置信度  : {s.get('confidence', 0)}/10")
    print(f"  建议    : {s.get('advice', '?')}")
    print()
    if s.get("current_positions"):
        print(f"  📦 持仓:")
        for p in s["current_positions"]:
            print(f"    {p}")
    else:
        print(f"  📭 无持仓")
    print()
    print(f"  📊 多空评分 | 多:{s.get('total_long_score',0)}  空:{s.get('total_short_score',0)}")
    print()
    if s.get("key_signals"):
        print(f"  🔑 关键信号:")
        for sig in s["key_signals"][:6]:
            print(f"    {sig}")
    # 余额
    bal = r.get("balance", {})
    print(f"  💰 USDT: {bal.get('free',0):.2f} 可用 / {bal.get('total',0):.2f} 总计")
    print("=" * 58)
except Exception as e:
    print(f"❌ 读取分析失败: {e}")
    import traceback; traceback.print_exc()
PYEOF

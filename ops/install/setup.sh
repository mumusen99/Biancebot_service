#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${PYTHON:-python3}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if [[ "${LIVE_TRADING_ENABLED:-}" != "YES" ]]; then
  echo "LIVE_TRADING_ENABLED=YES is required" >&2
  exit 1
fi

$PYTHON -m compileall -q "$ROOT/src" "$ROOT/main.py"
$PYTHON - <<'PYCODE'
from trading_bot.core.settings import API_KEY, API_SECRET
from trading_bot.exchange.market_data import fetch_ticker, fetch_balance
if not API_KEY or not API_SECRET:
    raise SystemExit("BINANCE_API_KEY/BINANCE_API_SECRET missing")
price = fetch_ticker(symbol="BTCUSDT")
print("BTC ticker:", price)
print("Balance:", fetch_balance())
PYCODE

echo "Setup check passed. Start with: python $ROOT/main.py"

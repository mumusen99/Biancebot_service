#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/../trading_bot_package.tar.gz}"
cd "$(dirname "$ROOT")"
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' -czf "$OUT" "$(basename "$ROOT")"
sha256sum "$OUT"

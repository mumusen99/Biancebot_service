#!/bin/bash
# ─────────────────────────────────────────────────────────
# KOL 针对性币种情绪分析 — 每4小时自动执行
# 对配置中的所有币种进行针对性网络检索分析
# ─────────────────────────────────────────────────────────
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$BASE_DIR/../binance-venv"
PYTHON="$VENV_DIR/bin/python3"

$PYTHON "$BASE_DIR/kol_coin_sentiment.py" >> "$BASE_DIR/cron.log" 2>&1

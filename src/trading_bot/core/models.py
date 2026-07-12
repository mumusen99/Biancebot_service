"""统一数据模型：持仓键、信号、状态迁移."""
from __future__ import annotations


def position_key(symbol: str, side: str) -> str:
    """生成统一持仓键: SYMBOL:SIDE"""
    symbol = symbol.upper().strip()
    side = side.upper().strip()
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"invalid position side: {side}")
    return f"{symbol}:{side}"


def split_position_key(key: str) -> tuple[str, str]:
    """拆分持仓键为 (symbol, side)"""
    symbol, side = key.rsplit(":", 1)
    return symbol.upper(), side.upper()


def migrate_position_keys(state: dict) -> dict:
    """启动时将旧 SYMBOL 键迁移为 SYMBOL:SIDE"""
    positions = state.get("positions", {})
    if not positions:
        return state

    migrated = {}
    for old_key, pos in positions.items():
        if ":" in old_key and old_key.split(":")[-1] in {"LONG", "SHORT"}:
            migrated[old_key] = pos
            continue
        symbol = str(pos.get("symbol") or old_key).upper()
        side = str(pos.get("side") or "LONG").upper()
        pos["symbol"] = symbol
        pos["side"] = side
        migrated[position_key(symbol, side)] = pos
    state["positions"] = migrated
    return state

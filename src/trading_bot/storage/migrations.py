"""
状态迁移脚本 — 将旧格式 bot_state.json 迁移到新 schema。

迁移规则 (per trading_bot_refactor_improvement_plan.md §P0-3):
1. 旧状态若只存在 symbol：尝试读取 state 中的 side
2. 转换为 symbol:side
3. 如果无法确定 side：查询交易所仓位，无法匹配则放入 orphaned_state
4. 备份原文件
5. 写入 schema_version
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trading_bot.migrations")

CURRENT_SCHEMA_VERSION = 2


def backup_state(state_path: Path) -> Path:
    """备份原状态文件。"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = state_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{state_path.name}.v1_backup_{ts}"
    shutil.copy2(state_path, backup_path)
    logger.info("backed up %s → %s", state_path, backup_path)
    return backup_path


def migrate_bot_state(state_path: Path) -> dict:
    """
    迁移 bot_state.json 到新 schema。

    返回迁移后的 state dict。
    """
    if not state_path.exists():
        logger.warning("state file not found: %s", state_path)
        return {"schema_version": CURRENT_SCHEMA_VERSION, "positions": {}}

    # 备份
    backup_state(state_path)

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error("failed to read state: %s", e)
        return {"schema_version": CURRENT_SCHEMA_VERSION, "positions": {}, "_migration_error": str(e)}

    # 如果已经是新 schema，跳过
    if state.get("schema_version", 0) >= CURRENT_SCHEMA_VERSION:
        logger.info("state already at schema_version %s", state.get("schema_version"))
        return state

    positions = state.get("positions", {})
    migrated = {}
    orphaned = {}

    for old_key, pos in list(positions.items()):
        # 已经使用 symbol:side 格式
        if ":" in old_key:
            parts = old_key.rsplit(":", 1)
            if parts[-1] in ("LONG", "SHORT"):
                migrated[old_key] = pos
                continue

        # 尝试从 position 数据中获取 side
        symbol = str(pos.get("symbol") or old_key).upper()
        side = str(pos.get("side") or pos.get("positionSide") or "").upper()

        if side in ("LONG", "SHORT"):
            new_key = f"{symbol}:{side}"
            pos["symbol"] = symbol
            pos["side"] = side
            migrated[new_key] = pos
            logger.info("migrated %s → %s", old_key, new_key)
        else:
            # 无法确定 side — 放入 orphaned
            orphaned[old_key] = pos
            logger.warning("orphaned position: %s (cannot determine side)", old_key)

    state["positions"] = migrated
    state["schema_version"] = CURRENT_SCHEMA_VERSION
    if orphaned:
        state["orphaned_positions"] = orphaned

    # 原子写入
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    with tmp_path.open("r+") as fh:
        fh.flush()
        os.fsync(fh.fileno())
    tmp_path.replace(state_path)

    logger.info("migration complete: %d positions, %d orphaned", len(migrated), len(orphaned))
    return state


def migrate_all_states(state_dir: Path) -> dict:
    """迁移 state_dir 下所有 bot_state.json 文件。"""
    results = {}
    for pattern in ("bot_state.json", "state.json"):
        p = state_dir / pattern
        if p.exists():
            try:
                results[pattern] = migrate_bot_state(p)
            except Exception as e:
                logger.error("migration failed for %s: %s", pattern, e)
                results[pattern] = {"error": str(e)}
    return results


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    state_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state")
    results = migrate_all_states(state_dir)
    print(json.dumps(results, indent=2))

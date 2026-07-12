"""
原子化状态存储 — 唯一的状态读写入口。

增强 (per §P1-3):
- 原子写入: tmp → fsync → replace
- 文件锁: flock 防止双进程并发写
- Schema version: 自动追踪
- 校验和: CRC32 防静默损坏
- 父目录 fsync: 确保 rename 持久化
- 读取失败: 回退 .bak，不静默返回空状态
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import struct
import zlib
from pathlib import Path
from typing import Optional

logger = logging.getLogger("trading_bot.state_store")

CURRENT_SCHEMA_VERSION = 2


def _checksum(data: str) -> str:
    """CRC32 校验和（hex）。"""
    return format(zlib.crc32(data.encode("utf-8")) & 0xFFFFFFFF, "08x")


def _fsync_dir(path: Path):
    """fsync 父目录以确保 rename 持久化。"""
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass


class StateStore:
    """唯一状态存储实现。"""

    def __init__(self, path: Path):
        self._path = path
        self._lock_path = path.with_suffix(path.suffix + ".lock")
        self._bak_path = path.with_suffix(path.suffix + ".bak")
        self._lock_fd: Optional[int] = None

    def acquire_lock(self, timeout: float = 2.0) -> bool:
        """获取排他文件锁。返回 True 表示成功。"""
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            return True
        except (OSError, BlockingIOError):
            logger.error("cannot acquire state lock — another process may be running")
            if fd:
                os.close(fd)
            return False

    def release_lock(self):
        """释放文件锁。"""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def load(self) -> dict:
        """安全读取状态：主文件 → .bak → 抛出异常。不静默返回空状态。"""
        for p in (self._path, self._bak_path):
            try:
                if not p.exists():
                    continue
                raw = p.read_text(encoding="utf-8")
                state = json.loads(raw)

                # 校验 checksum（如果有）
                stored_csum = state.pop("_checksum", None)
                if stored_csum:
                    # 重新计算不含 _checksum 的校验和
                    recomputed = _checksum(json.dumps(state, sort_keys=True, ensure_ascii=False))
                    if recomputed != stored_csum:
                        logger.warning("checksum mismatch for %s, trying backup", p)
                        continue
                    state["_checksum"] = stored_csum  # 恢复

                logger.debug("loaded state from %s (schema v%d)", p, state.get("schema_version", 0))
                return state
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("state read failed for %s: %s", p, e)
                continue

        raise RuntimeError(
            f"state file {self._path} and backup are both unreadable or corrupt. "
            "manual intervention required."
        )

    def save(self, state: dict) -> None:
        """原子写入状态：校验 → tmp → fsync → replace → fsync dir。"""
        if self._lock_fd is None:
            logger.debug("saving state without lock held")

        # Schema version
        state["schema_version"] = CURRENT_SCHEMA_VERSION

        # 计算校验和（排除 _checksum 自身）
        clean = {k: v for k, v in state.items() if k != "_checksum"}
        csum = _checksum(json.dumps(clean, sort_keys=True, ensure_ascii=False))
        state["_checksum"] = csum

        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        content = json.dumps(state, ensure_ascii=False, indent=2)

        # 写入临时文件
        tmp.write_text(content, encoding="utf-8")
        with tmp.open("r+") as fh:
            fh.flush()
            os.fsync(fh.fileno())

        # 备份旧文件
        try:
            if self._path.exists():
                self._path.replace(self._bak_path)
        except OSError:
            pass

        # 原子替换
        tmp.replace(self._path)
        _fsync_dir(self._path)

        logger.debug("saved state (schema v%d, csum=%s)", CURRENT_SCHEMA_VERSION, csum)

    def transaction(self):
        """上下文管理器：自动获取/释放锁。"""
        return _StateTransaction(self)


class _StateTransaction:
    def __init__(self, store: StateStore):
        self._store = store
        self._state: Optional[dict] = None

    def __enter__(self):
        if not self._store.acquire_lock():
            raise RuntimeError("cannot acquire state lock")
        self._state = self._store.load()
        return self._state

    def __exit__(self, *args):
        try:
            if self._state is not None:
                self._store.save(self._state)
        finally:
            self._store.release_lock()


# ─── 便捷函数（兼容旧接口）───

_default_store: Optional[StateStore] = None


def _get_default_path() -> Path:
    from trading_bot.core.settings import BOT_STATE_FILE
    return BOT_STATE_FILE


def get_state_store() -> StateStore:
    global _default_store
    if _default_store is None:
        _default_store = StateStore(_get_default_path())
    return _default_store


def save_state_atomic(path: Path, state: dict) -> None:
    """兼容旧接口的原子写入。"""
    store = StateStore(path)
    store.save(state)


def load_state_safe(path: Path) -> dict:
    """兼容旧接口的安全读取。"""
    store = StateStore(path)
    try:
        return store.load()
    except RuntimeError:
        logger.error("state corrupted, returning empty safe state")
        return {"positions": {}, "trades": [], "total_pnl": 0.0, "budget": 50.0}

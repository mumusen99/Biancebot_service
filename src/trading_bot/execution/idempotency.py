"""
持久化幂等控制 — 防止重启后重复开仓。

原则 (per §P1-4):
1. 创建订单前持久化 PENDING
2. 发送请求
3. 超时时先查询，不得直接重发
4. 确认交易所订单后写 CONFIRMED
5. 重启时恢复所有 PENDING
6. clientOrderId 与幂等键关联
7. 重复调用返回已有结果

幂等键格式: entry:{symbol}:{side}:{signal_time_bucket}:{strategy_version}
             protection:{position_id}:{role}:{version}
             exit:{position_id}:{reason}:{version}
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Optional

logger = logging.getLogger("trading_bot.idempotency")

IDEMPOTENCY_FILE = Path(os.getenv("TRADING_STATE_DIR", "state")) / "idempotency.json"
PENDING_TTL_SECONDS = 300  # PENDING 状态最大存活时间


@dataclass
class IdempotencyRecord:
    key: str
    operation: str          # entry / protection / exit
    status: str             # PENDING / CONFIRMED / FAILED
    created_at: float       # epoch
    expires_at: float       # epoch
    exchange_order_id: str | None = None
    client_order_id: str | None = None
    result_hash: str | None = None
    symbol: str = ""
    side: str = ""


class IdempotencyStore:
    """文件持久化的幂等存储。"""

    def __init__(self, file_path: Path = IDEMPOTENCY_FILE):
        self._file = file_path
        self._lock = RLock()
        self._records: dict[str, IdempotencyRecord] = {}
        self._load()

    def _load(self):
        """从磁盘加载已有记录。"""
        if not self._file.exists():
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
            for k, v in raw.get("records", {}).items():
                self._records[k] = IdempotencyRecord(**v)
            logger.info("loaded %d idempotency records", len(self._records))
        except Exception as e:
            logger.warning("failed to load idempotency: %s", e)

    def _save(self):
        """原子写入磁盘。"""
        try:
            data = {
                "schema_version": 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "records": {k: {
                    "key": r.key, "operation": r.operation, "status": r.status,
                    "created_at": r.created_at, "expires_at": r.expires_at,
                    "exchange_order_id": r.exchange_order_id,
                    "client_order_id": r.client_order_id,
                    "result_hash": r.result_hash,
                    "symbol": r.symbol, "side": r.side,
                } for k, r in self._records.items()},
            }
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            with tmp.open("r+") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self._file)
        except Exception as e:
            logger.error("failed to save idempotency: %s", e)

    def _cleanup(self):
        """清理过期记录。"""
        now = time.time()
        expired = [k for k, r in self._records.items() if r.expires_at < now]
        for k in expired:
            logger.debug("idempotency expired: %s (%s)", k, self._records[k].status)
            del self._records[k]
        if expired:
            self._save()

    def is_duplicate(self, key: str) -> bool:
        """检查是否已存在（CONFIRMED/PENDING 都算重复）。"""
        with self._lock:
            self._cleanup()
            rec = self._records.get(key)
            if rec is None:
                return False
            if rec.status == "FAILED":
                return False  # 失败的可重试
            return True  # PENDING 或 CONFIRMED 都阻止重复

    def mark_pending(self, key: str, operation: str, *,
                     symbol: str = "", side: str = "",
                     client_order_id: str = "") -> IdempotencyRecord:
        """标记为 PENDING（下单前调用）。如果已存在则返回已有记录。"""
        with self._lock:
            self._cleanup()
            existing = self._records.get(key)
            if existing and existing.status != "FAILED":
                return existing
            rec = IdempotencyRecord(
                key=key, operation=operation, status="PENDING",
                created_at=time.time(),
                expires_at=time.time() + PENDING_TTL_SECONDS,
                client_order_id=client_order_id,
                symbol=symbol, side=side,
            )
            self._records[key] = rec
            self._save()
            logger.info("PENDING %s %s %s", operation, key, client_order_id)
            return rec

    def mark_confirmed(self, key: str, exchange_order_id: str,
                       result_hash: str = "") -> IdempotencyRecord | None:
        """标记为 CONFIRMED（下单成功后调用）。"""
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                return None
            rec.status = "CONFIRMED"
            rec.exchange_order_id = exchange_order_id
            rec.result_hash = result_hash
            rec.expires_at = time.time() + 86400  # 确认后保留24h
            self._save()
            logger.info("CONFIRMED %s %s orderId=%s", rec.operation, key, exchange_order_id)
            return rec

    def mark_failed(self, key: str, reason: str = "") -> IdempotencyRecord | None:
        """标记为 FAILED（下单失败后调用）。"""
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                return None
            rec.status = "FAILED"
            rec.result_hash = reason
            rec.expires_at = time.time() + 60  # 失败记录保留1分钟后可重试
            self._save()
            logger.warning("FAILED %s %s: %s", rec.operation, key, reason)
            return rec

    def get_pending(self) -> list[IdempotencyRecord]:
        """获取所有 PENDING 记录（重启恢复用）。"""
        with self._lock:
            self._cleanup()
            return [r for r in self._records.values() if r.status == "PENDING"]

    def recover_pending(self) -> list[dict]:
        """
        重启时恢复 PENDING 记录。
        返回需要通过交易所查询确认的订单列表。
        """
        pending = self.get_pending()
        result = []
        for rec in pending:
            age = time.time() - rec.created_at
            if age > 60:  # 超过60秒的PENDING，需要查询交易所确认
                result.append({
                    "action": "query_exchange",
                    "key": rec.key,
                    "client_order_id": rec.client_order_id,
                    "symbol": rec.symbol,
                    "age_seconds": age,
                })
            else:
                result.append({
                    "action": "wait",
                    "key": rec.key,
                    "client_order_id": rec.client_order_id,
                    "age_seconds": age,
                })
        logger.info("recover: %d pending, %d need query", len(pending), 
                   sum(1 for r in result if r["action"] == "query_exchange"))
        return result


# ─── 全局单例 ───

_store: Optional[IdempotencyStore] = None


def get_idempotency_store() -> IdempotencyStore:
    global _store
    if _store is None:
        _store = IdempotencyStore()
    return _store

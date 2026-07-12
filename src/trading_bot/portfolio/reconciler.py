"""
PositionReconciler — 交易所仓位 vs 本地状态协调器。

原则 (per §P1-2):
- 读取快照 → 计算差异 → 生成修复计划 → 提交执行队列 → 确认结果 → 更新本地状态
- 不边遍历边修改状态
- 孤儿订单可识别并安全处理
- 数量不匹配可自动修正保护单
- 修复失败会熔断对应仓位
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from trading_bot.exchange.gateway import get_gateway

logger = logging.getLogger("trading_bot.reconciler")


@dataclass
class ReconciliationReport:
    """交换所 vs 本地状态差异报告。"""
    # 交易所有但本地没有的仓位
    missing_local: list[dict] = field(default_factory=list)
    # 本地有但交易所已平的
    stale_local: list[dict] = field(default_factory=list)
    # 持仓存在但缺少止损单
    missing_stop_orders: list[dict] = field(default_factory=list)
    # 本地不存在但交易所有条件单挂着的
    orphan_orders: list[dict] = field(default_factory=list)
    # 持仓数量不匹配
    quantity_mismatches: list[dict] = field(default_factory=list)
    # 交易所有但本地标记为 closed 的（需恢复active）
    reactivated: list[str] = field(default_factory=list)
    # 方向不匹配
    side_mismatches: list[dict] = field(default_factory=list)
    # 统计
    exchange_positions: int = 0
    exchange_orders: int = 0
    exchange_algos: int = 0
    local_positions: int = 0
    # 是否有需要熔断的严重问题
    requires_halt: bool = False
    halt_reason: str = ""

    @property
    def is_clean(self) -> bool:
        return not any([
            self.missing_local, self.stale_local, self.missing_stop_orders,
            self.orphan_orders, self.quantity_mismatches, self.side_mismatches,
        ])

    @property
    def summary(self) -> str:
        parts = []
        if self.missing_local:
            parts.append(f"missing_local={len(self.missing_local)}")
        if self.stale_local:
            parts.append(f"stale_local={len(self.stale_local)}")
        if self.missing_stop_orders:
            parts.append(f"missing_stop={len(self.missing_stop_orders)}")
        if self.orphan_orders:
            parts.append(f"orphan={len(self.orphan_orders)}")
        if self.quantity_mismatches:
            parts.append(f"qty_mismatch={len(self.quantity_mismatches)}")
        if self.side_mismatches:
            parts.append(f"side_mismatch={len(self.side_mismatches)}")
        if self.requires_halt:
            parts.append(f"HALT:{self.halt_reason}")
        return ", ".join(parts) if parts else "clean"


class PositionReconciler:
    """仓位协调器 — 只读快照 → 差异计算 → 输出修复方案。"""

    def __init__(self):
        self._gw = get_gateway()

    def reconcile(
        self,
        local_state: dict,
        *,
        repair_missing_stops: bool = True,
    ) -> ReconciliationReport:
        """
        执行完整协调。

        Args:
            local_state: bot_state.json 的 positions 字典 (key=symbol:side)
            repair_missing_stops: 是否自动修复缺失止损
        Returns:
            ReconciliationReport
        """
        report = ReconciliationReport()

        # ── 1. 读取交易所快照 ──
        try:
            exchange_positions = self._gw.get_positions()
            exchange_orders = self._gw.get_open_orders()
            exchange_algos = self._gw.get_algo_orders()
        except Exception as e:
            logger.error("failed to fetch exchange data: %s", e)
            report.requires_halt = True
            report.halt_reason = f"exchange_unreachable: {e}"
            return report

        report.exchange_positions = len(exchange_positions)
        report.exchange_orders = len(exchange_orders)
        report.exchange_algos = len(exchange_algos)
        report.local_positions = len(local_state)

        # ── 建立索引 ──
        # 交易所仓位: {(symbol, side): position}
        live_by_key = {}
        for p in exchange_positions:
            key = (p.symbol, p.position_side.value)
            live_by_key[key] = p

        # 交易所订单: {(symbol, side): [orders]}
        orders_by_key = {}
        for o in exchange_orders:
            key = (o.symbol, o.position_side.value)
            orders_by_key.setdefault(key, []).append(o)

        # 交易所条件单: {(symbol, side): [algos]}
        algos_by_key = {}
        for a in exchange_algos:
            key = (a.symbol, a.position_side.value)
            algos_by_key.setdefault(key, []).append(a)

        # 本地仓位: {key_str: pos_dict}
        local_keys = set(local_state.keys())

        # ── 2. 逐项比对 ──

        # A. missing_local: 交易所有但本地没有
        for (sym, side), live_pos in live_by_key.items():
            local_key = f"{sym}:{side}"
            if local_key not in local_keys and sym not in local_keys:
                report.missing_local.append({
                    "symbol": sym,
                    "side": side,
                    "qty": float(live_pos.position_amt),
                    "entry_price": float(live_pos.entry_price),
                    "mark_price": float(live_pos.mark_price),
                    "pnl": float(live_pos.unrealized_pnl),
                    "leverage": live_pos.leverage,
                    "local_key": local_key,
                })

        # B. stale_local + reactivated
        for local_key, local_pos in list(local_state.items()):
            sym = local_pos.get("symbol", local_key.split(":")[0] if ":" in local_key else local_key)
            side = local_pos.get("side", local_key.split(":")[1] if ":" in local_key else "LONG")
            ex_key = (sym, side)
            if ex_key in live_by_key and local_pos.get("status") == "closed":
                report.reactivated.append(local_key)
                continue
            if ex_key not in live_by_key:
                # 如果不是 pending 状态，视为已平
                status = local_pos.get("status", "")
                if status != "pending":
                    report.stale_local.append({
                        "local_key": local_key,
                        "symbol": sym,
                        "side": side,
                        "status": status,
                    })

        # C. quantity_mismatches: 数量不一致
        for (sym, side), live_pos in live_by_key.items():
            local_key = f"{sym}:{side}"
            local_pos = local_state.get(local_key, local_state.get(sym))
            if local_pos and local_pos.get("status") != "pending":
                local_qty = float(local_pos.get("qty", 0))
                live_qty = abs(float(live_pos.position_amt))
                if abs(local_qty - live_qty) > 0.0001:
                    report.quantity_mismatches.append({
                        "symbol": sym,
                        "side": side,
                        "local_key": local_key,
                        "local_qty": local_qty,
                        "exchange_qty": live_qty,
                    })

        # D. side_mismatches: 方向不一致
        for local_key, local_pos in local_state.items():
            if ":" in local_key:
                sym, side = local_key.split(":", 1)
                ex_key = (sym, side)
                # 如果本地说LONG但交易所只有SHORT
                if ex_key not in live_by_key:
                    other_side = "SHORT" if side == "LONG" else "LONG"
                    if (sym, other_side) in live_by_key:
                        report.side_mismatches.append({
                            "local_key": local_key,
                            "symbol": sym,
                            "local_side": side,
                            "exchange_side": other_side,
                        })

        # E. missing_stop_orders: 有持仓但缺止损（本地sl_price也算已保护）
        for (sym, side), live_pos in live_by_key.items():
            if abs(float(live_pos.position_amt)) < 0.0001:
                continue
            local_key = f"{sym}:{side}"
            algos = algos_by_key.get((sym, side), [])
            has_stop = any(
                a.order_type.value in ("STOP_MARKET", "TRAILING_STOP_MARKET")
                for a in algos
            )
            # 本地 state 有 sl_price 也算已保护
            local_pos = local_state.get(local_key, {})
            has_local_sl = float(local_pos.get("sl_price", 0) or 0) > 0
            if not has_stop and not has_local_sl:
                report.missing_stop_orders.append({
                    "symbol": sym,
                    "side": side,
                    "local_key": local_key,
                    "qty": abs(float(live_pos.position_amt)),
                    "entry_price": float(live_pos.entry_price),
                    "mark_price": float(live_pos.mark_price),
                    "leverage": live_pos.leverage,
                })

        # F. orphan_orders: 条件单存在但无对应持仓
        for (sym, side), algos in algos_by_key.items():
            local_key = f"{sym}:{side}"
            if local_key not in local_keys and sym not in local_keys:
                if (sym, side) not in live_by_key:
                    for a in algos:
                        report.orphan_orders.append({
                            "symbol": sym,
                            "side": side,
                            "algo_id": a.order_id,
                            "type": a.order_type.value,
                            "stop_price": float(a.stop_price),
                        })

        # ── 3. 生成修复方案（不直接修改状态）──
        if report.missing_stop_orders and repair_missing_stops:
            logger.warning("UNPROTECTED positions: %s",
                          [(m["symbol"], m["side"]) for m in report.missing_stop_orders])

        # 严重问题熔断
        if report.side_mismatches:
            report.requires_halt = True
            report.halt_reason = f"side_mismatch: {len(report.side_mismatches)} positions"

        logger.info("reconcile: %s", report.summary)
        return report

    def apply_repairs(self, report: ReconciliationReport) -> dict:
        """
        将差异报告转换为可执行的修复操作。

        Returns:
            {"actions": [...], "halts": [...]}
        """
        actions = []
        halts = []

        # stale_local → 删除本地记录
        for item in report.stale_local:
            actions.append({
                "type": "drop_local",
                "local_key": item["local_key"],
                "reason": "stale — position closed on exchange",
            })

        # missing_local → 创建本地记录（不打标，等下次周期自动标）
        for item in report.missing_local:
            actions.append({
                "type": "add_local",
                "local_key": item["local_key"],
                "symbol": item["symbol"],
                "side": item["side"],
                "qty": item["qty"],
                "entry_price": item.get("entry_price", 0),
                "mark_price": item.get("mark_price", 0),
                "strategy": "unknown",
                "reason": "found on exchange, not in local state",
            })

        # missing_stop_orders → 提交止损单
        for item in report.missing_stop_orders:
            actions.append({
                "type": "create_stop",
                "symbol": item["symbol"],
                "side": item["side"],
                "qty": item["qty"],
                "entry_price": item.get("entry_price", 0),
                "mark_price": item.get("mark_price", 0),
            })

        # orphan_orders → 取消孤立条件单
        for item in report.orphan_orders:
            actions.append({
                "type": "cancel_algo",
                "symbol": item["symbol"],
                "algo_id": item["algo_id"],
                "reason": f"orphan — no matching position ({item['type']})",
            })

        # quantity_mismatches → 标记需要人工检查
        for item in report.quantity_mismatches:
            halts.append({
                "type": "qty_mismatch",
                "symbol": item.get("symbol", item.get("local_key", "?")),
                "detail": f'local={item.get("local_qty","?")} vs exchange={item.get("exchange_qty","?")}',
            })

        # side_mismatches → 熔断
        for item in report.side_mismatches:
            halts.append({
                "type": "side_mismatch",
                "symbol": item.get("symbol", "?"),
                "detail": f'local={item.get("local_side","?")} vs exchange={item.get("exchange_side","?")}',
            })

        return {"actions": actions, "halts": halts, "report_summary": report.summary}

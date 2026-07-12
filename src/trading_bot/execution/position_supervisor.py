"""Position supervisor — unified position monitoring: SL/TP/trailing/timeout."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from trading_bot.data.ws_market_client import market_cache

logger = logging.getLogger("trading_bot.supervisor")


@dataclass
class ExitDecision:
    """Exit decision from supervisor evaluation."""
    symbol: str
    side: str
    action: str  # "SL" | "TP1" | "TP2" | "TRAIL" | "TIME"
    price: float
    qty: float
    pnl: float = 0.0

    @classmethod
    def hold(cls) -> "ExitDecision":
        return cls("", "", "HOLD", 0, 0)


class PositionSupervisor:
    """Unified exit evaluation — generates ExitDecisions, does NOT place orders."""

    def __init__(self, state_store):
        self.store = state_store

    def _get_price(self, sym: str, pos: dict) -> Optional[float]:
        bt = market_cache.get_book_ticker(sym)
        if bt:
            return (bt['b'] + bt['a']) / 2
        cached = float(pos.get('current_price', 0) or pos.get('entry_price', 0))
        return cached if cached > 0 else None

    def evaluate(self, key: str, pos: dict) -> ExitDecision:
        """Evaluate one position. Returns ExitDecision or HOLD."""
        sym = pos.get('symbol', key.split(':')[0])
        side = pos.get('side', 'LONG')
        entry = float(pos.get('entry_price', 0))
        if entry <= 0:
            return ExitDecision.hold()

        price = self._get_price(sym, pos)
        if not price:
            return ExitDecision.hold()

        sl = float(pos.get('sl_price', 0) or 0)
        cur_qty = float(pos.get('qty', pos.get('original_qty', 0)) or 0)
        if cur_qty <= 0:
            return ExitDecision.hold()

        # Ensure SL exists
        if sl <= 0:
            sl = round(entry * (0.995 if side == 'LONG' else 1.005), 8)
            pos['sl_price'] = sl

        risk_dist = abs(entry - sl)
        if risk_dist <= 0:
            return ExitDecision.hold()

        # ── Cost floor: SL must cover at least 2x estimated costs (0.04% per side) ──
        cost_floor = entry * 0.0008  # 0.08% round-trip
        if risk_dist < cost_floor and risk_dist > 0:
            sl = round(entry - cost_floor if side == 'LONG' else entry + cost_floor, 8)
            pos['sl_price'] = sl
            risk_dist = cost_floor

        # ── SL check (2-confirm) ──
        hit_sl = (side == 'LONG' and price <= sl) or (side == 'SHORT' and price >= sl)
        old_sl = int(pos.get('_sl_confirm', 0))
        sl_conf = old_sl + 1 if hit_sl else 0
        if sl_conf != old_sl:
            pos['_sl_confirm'] = sl_conf
        if sl_conf >= 2:
            pnl = (price - entry) * cur_qty if side == 'LONG' else (entry - price) * cur_qty
            logger.warning(f'🛑 SL: {sym} price={price} sl={sl}')
            return ExitDecision(sym, side, "SL", price, cur_qty, pnl)

        # ── TP1 (50%) ──
        tp1_raw = 1.0 * risk_dist
        tp1_min = cost_floor * 2.5  # TP1 must cover at least 2.5x costs
        tp1_dist = max(tp1_raw, tp1_min)
        tp1 = round(entry + tp1_dist if side == 'LONG' else entry - tp1_dist, 8)
        if not pos.get('tp1_hit'):
            hit = (side == 'LONG' and price >= tp1) or (side == 'SHORT' and price <= tp1)
            old_c = int(pos.get('_tp1_confirm', 0))
            c = old_c + 1 if hit else 0
            if c != old_c:
                pos['_tp1_confirm'] = c
            if c >= 2:
                qty1 = max(1, int(cur_qty * 0.50))
                pos['tp1_hit'] = True
                pos['qty'] = cur_qty - qty1
                pos['trailing_active'] = True
                pos['highest_price'] = price
                logger.warning(f'🎯 TP1: {sym} 50%({qty1})')
                if pos['qty'] <= 0:
                    pnl = (price - entry) * cur_qty if side == 'LONG' else (entry - price) * cur_qty
                    return ExitDecision(sym, side, "TP1", price, qty1, pnl)
                return ExitDecision(sym, side, "TP1", price, qty1)

        # ── TP2 (30%) ──
        tp2 = round(entry + 1.5 * risk_dist if side == 'LONG' else entry - 1.5 * risk_dist, 8)
        if not pos.get('tp2_hit'):
            hit = (side == 'LONG' and price >= tp2) or (side == 'SHORT' and price <= tp2)
            old_c2 = int(pos.get('_tp2_confirm', 0))
            c2 = old_c2 + 1 if hit else 0
            if c2 != old_c2:
                pos['_tp2_confirm'] = c2
            if c2 >= 2:
                qty2 = max(1, int(cur_qty * 0.30))
                pos['tp2_hit'] = True
                pos['qty'] = max(0, cur_qty - qty2)
                logger.warning(f'🎯 TP2: {sym} 30%({qty2})')
                if pos['qty'] <= 0:
                    pnl = (price - entry) * cur_qty if side == 'LONG' else (entry - price) * cur_qty
                    return ExitDecision(sym, side, "TP2", price, qty2, pnl)
                return ExitDecision(sym, side, "TP2", price, qty2)

        # ── Trailing stop (runner) ──
        tp3 = round(entry + 2.5 * risk_dist if side == 'LONG' else entry - 2.5 * risk_dist, 8)
        trail_dist = round(risk_dist * 0.5, 8)
        if pos.get('trailing_active') or (side == 'LONG' and price >= tp3) or (side == 'SHORT' and price <= tp3):
            pos['trailing_active'] = True
            highest = float(pos.get('highest_price', price))
            if (side == 'LONG' and price > highest) or (side == 'SHORT' and price < highest):
                pos['highest_price'] = price
                pos['_trail_confirm'] = 0
            hit_trail = (side == 'LONG' and price <= highest - trail_dist) or (side == 'SHORT' and price >= highest + trail_dist)
            old_tc = int(pos.get('_trail_confirm', 0))
            tc = old_tc + 1 if hit_trail else 0
            if tc != old_tc:
                pos['_trail_confirm'] = tc
            if tc >= 2:
                runner_qty = float(pos.get('qty', cur_qty))
                pnl = (price - entry) * runner_qty if side == 'LONG' else (entry - price) * runner_qty
                logger.warning(f'🏃 Trail: {sym} from {highest} to {price}')
                pos['qty'] = 0
                return ExitDecision(sym, side, "TRAIL", price, runner_qty, pnl)

        # ── Time stop ──
        from datetime import datetime, timezone
        opened_str = pos.get('opened_at', '')
        if opened_str:
            try:
                opened_dt = datetime.fromisoformat(opened_str)
                held = (datetime.now(timezone.utc) - opened_dt).total_seconds()
                mfe = float(pos.get('mfe', 0) or 0)
                if held > 1500 and mfe < entry * cur_qty * 0.0015:
                    pnl = (price - entry) * cur_qty if side == 'LONG' else (entry - price) * cur_qty
                    logger.warning(f'⏰ TIME: {sym} held={held:.0f}s mfe={mfe:.4f}')
                    return ExitDecision(sym, side, "TIME", price, cur_qty, pnl)
            except Exception:
                pass

        return ExitDecision.hold()

    def evaluate_all(self, state: dict) -> tuple[list[ExitDecision], dict]:
        """Evaluate all active positions, return (decisions, updated_state)."""
        decisions = []
        changed = False
        positions = state.get('positions', {})

        for key, pos in list(positions.items()):
            if pos.get('status') not in ('active', 'pending'):
                continue
            d = self.evaluate(key, pos)
            if d.action == "HOLD":
                continue
            decisions.append(d)
            if d.action in ("SL", "TRAIL") or (d.action in ("TP1", "TP2") and pos.get('qty', 0) <= 0):
                pos['status'] = 'closed'
            changed = True

        return decisions, state

"""Reconcile broker positions with stored records after start-up, reconnects and on a timer.

Broker state is the source of truth for what is open and what the stop is; the journal is the source for
trailing context (water marks, original risk). Reconciliation never resets a water mark in a way that weakens
protection, adopts unexpected bot-magic positions, completes the record of positions that closed while the
engine was offline, and re-protects any bot position that has no stop.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .gateways import Gateway
from .models import (
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_REASON_SL, DEAL_REASON_SO, DEAL_REASON_TP, DEAL_REASON_CLIENT, DEAL_REASON_EXPERT,
    Deal, Position, SymbolSpec,
)
from .monitoring_and_alerts import Alerts
from .order_executor import OrderExecutor
from .trade_journal import Journal, now_iso
from .trailing_stop_manager import TrailState, TrailingManager

log = logging.getLogger("reconcile")


def position_row(mode: str, p: Position, instrument: str, st: Optional[TrailState], signal_id: Optional[str] = None,
                 meta: Optional[dict] = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "mode": mode, "ticket": p.ticket, "signal_id": signal_id, "instrument": instrument, "broker_symbol": p.symbol,
        "side": p.side, "volume": p.volume, "entry_price": p.price_open, "sl": p.sl or None, "tp": p.tp or None,
        "current_price": p.price_current or None, "floating_pnl": round(p.profit + p.swap, 2), "status": "open",
    }
    if st is not None:
        row.update({"original_sl": st.original_sl or None, "original_risk": st.original_risk, "high_water": st.high_water or None,
                    "low_water": st.low_water or None, "trail_active": st.active, "breakeven_done": st.break_even_done,
                    "last_ack_sl": st.last_ack_sl or None})
    if meta:
        row["meta"] = meta
    return row


def close_summary(row: dict, deals: list[Deal]) -> dict[str, Any]:
    """Build the closing update for a stored position from broker deals."""
    outs = [d for d in deals if d.entry in (DEAL_ENTRY_OUT, 3)]
    if not outs:
        return {"status": "closed", "closed_at": now_iso(), "close_reason": "closed (details unavailable)", "pnl": 0.0}
    price = outs[-1].price
    pnl = sum(d.profit + d.commission + d.swap + d.fee for d in deals)
    fees = sum(d.commission + d.fee for d in deals)
    reason = outs[-1].reason
    if reason == DEAL_REASON_SL:
        moved = row.get("original_sl") and row.get("last_ack_sl") and abs(row["last_ack_sl"] - row["original_sl"]) > 1e-9
        label = "trailing_stop" if moved else "stop_loss"
    elif reason == DEAL_REASON_TP:
        label = "take_profit"
    elif reason == DEAL_REASON_SO:
        label = "stop_out"
    elif reason in (DEAL_REASON_CLIENT, DEAL_REASON_EXPERT):
        label = "manual_or_bot_close"
    else:
        label = f"closed_reason_{reason}"
    return {"status": "closed", "closed_at": now_iso(), "close_price": price, "pnl": round(pnl, 2), "fees": round(fees, 2),
            "close_reason": label}


@dataclass
class ReconcileReport:
    adopted: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    protected: list[int] = field(default_factory=list)
    readable: bool = True


class PositionReconciler:
    def __init__(
        self, journal: Journal, trailing: TrailingManager, executor: OrderExecutor, alerts: Alerts,
        instrument_of: Callable[[str], Optional[str]], spec_for: Callable[[str], Optional[SymbolSpec]],
        atr_for: Callable[[str], Optional[float]], default_sl_atr: Callable[[str], float],
        on_closed: Callable[[str, dict, dict], None], stop_retries: int = 3,
    ):
        self.j, self.trailing, self.ex, self.alerts = journal, trailing, executor, alerts
        self._instrument_of, self._spec_for, self._atr_for = instrument_of, spec_for, atr_for
        self._default_sl_atr, self._on_closed, self._stop_retries = default_sl_atr, on_closed, stop_retries

    def reconcile(self, gw: Gateway) -> ReconcileReport:
        rep = ReconcileReport()
        mode = gw.mode
        live = gw.positions()
        if live is None:
            rep.readable = False
            self.alerts.emit("warn", "reconcile_unreadable", "Could not read positions from the broker; will retry", {"mode": mode})
            return rep
        stored = {int(r["ticket"]): r for r in self.j.positions(mode, "open")}
        live_by_ticket = {p.ticket: p for p in live}

        for p in live:
            instr = self._instrument_of(p.symbol) or p.symbol
            row = stored.get(p.ticket)
            atr_v = self._atr_for(p.symbol) or 0.0
            if row is None:
                rep.adopted.append(p.ticket)
                self.alerts.emit("warn", "unexpected_position", f"{instr} #{p.ticket} ({p.side} {p.volume}) was not in the journal; adopted for management", {"ticket": p.ticket})
                st = self.trailing.register(mode, p, atr_v or abs(p.price_open - p.sl) / 1.3 or 0.0, original_sl=p.sl or None)
                self.j.upsert_position(position_row(mode, p, instr, st, None, {"adopted": True, "atr_entry": atr_v}))
            else:
                st = self.trailing.register(
                    mode, p, float((row.get("meta") or {}).get("atr_entry") or atr_v), original_sl=row.get("original_sl"),
                    original_risk=float(row.get("original_risk") or 0), high_water=float(row.get("high_water") or 0),
                    low_water=float(row.get("low_water") or 0), active=bool(row.get("trail_active")),
                    break_even_done=bool(row.get("breakeven_done")))
            if p.sl <= 0:
                self._reprotect(gw, p, instr, st, rep)

        for ticket, row in stored.items():
            if ticket in live_by_ticket:
                continue
            deals = gw.deals_for_position(ticket)
            upd = close_summary(row, deals or [])
            if deals is None:
                upd["close_reason"] = "closed while engine could not read history"
                self.alerts.emit("warn", "close_unverified", f"#{ticket} no longer open; close details unavailable", {"ticket": ticket})
            self.j.upsert_position({"mode": mode, "ticket": ticket, **upd})
            self.trailing.forget(mode, ticket)
            rep.closed.append(ticket)
            self._on_closed(mode, row, upd)

        self._resolve_pending_executions(gw, live)
        return rep

    def _reprotect(self, gw: Gateway, p: Position, instr: str, st: TrailState, rep: ReconcileReport) -> None:
        spec = self._spec_for(p.symbol)
        atr_v = self._atr_for(p.symbol) or st.atr_entry
        if spec is None or not atr_v:
            self.alerts.emit("critical", "unprotected_open", f"{instr} #{p.ticket} has no stop and no ATR is available to set one. Act in MT5.", {"ticket": p.ticket})
            return
        dist = self._default_sl_atr(p.symbol) * atr_v
        planned = p.price_open - dist if p.side == "BUY" else p.price_open + dist
        self.alerts.emit("critical", "missing_stop", f"{instr} #{p.ticket} has no stop; protecting it", {"ticket": p.ticket})
        out = self.ex.recover_protection(gw, spec, p, planned, p.tp, self._stop_retries)
        if out == "RECOVERED":
            rep.protected.append(p.ticket)
            fresh = gw.position(p.ticket)
            if fresh:
                p.sl = fresh.sl
                st.original_sl = st.original_sl or fresh.sl
                st.last_ack_sl = fresh.sl
        elif out == "CLOSED":
            deals = gw.deals_for_position(p.ticket) or []
            upd = close_summary({}, deals)
            upd["close_reason"] = "closed: no protective stop"
            self.j.upsert_position({"mode": gw.mode, "ticket": p.ticket, **upd})
            self.trailing.forget(gw.mode, p.ticket)
            rep.closed.append(p.ticket)
            self._on_closed(gw.mode, {"ticket": p.ticket, "instrument": instr}, upd)

    def _resolve_pending_executions(self, gw: Gateway, live: list[Position]) -> None:
        """INTENT/SENT/AMBIGUOUS rows left by a crash or timeout are matched to broker reality, never resent."""
        for ex in self.j.unresolved_executions(gw.mode):
            comment = (ex.get("detail") or {}).get("comment", "")
            pos, deals = gw.find_by_comment(comment, time.time() - 86400) if comment else (None, [])
            if pos is not None or any(d.entry == DEAL_ENTRY_IN for d in deals):
                self.j.upsert_execution({"signal_id": ex["signal_id"], "mode": gw.mode, "state": "FILLED",
                                         "position_ticket": pos.ticket if pos else None, "error": "resolved by reconciliation"})
                self.alerts.emit("info", "execution_resolved", f"Pending order {comment} resolved: it was filled", {})
            else:
                age = time.time() - _parse_ts(ex.get("created_at"))
                if age > 180:
                    self.j.upsert_execution({"signal_id": ex["signal_id"], "mode": gw.mode, "state": "UNFILLED",
                                             "error": "no fill found at the broker after reconciliation"})
                    self.alerts.emit("info", "execution_resolved", f"Pending order {comment} resolved: no fill found", {})


def _parse_ts(s: Optional[str]) -> float:
    if not s:
        return 0.0
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0

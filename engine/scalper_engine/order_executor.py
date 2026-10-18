"""Order execution with verification, ambiguity handling and protective-stop recovery.

Pipeline for every entry (live and paper share it; only the gateway differs):
  authorize -> verify terminal/account -> fresh quote, spread, market status -> signal freshness + dedupe ->
  size from broker contract data -> risk gates + margin -> order_check -> durable INTENT -> order_send ->
  verify order/deal/position/fill -> verify (or establish) the protective stop.

Rules enforced here:
  * no durable INTENT record, no order;
  * a timeout or unknown result is never resent: the engine reconciles by unique comment first;
  * a filled position without a stop triggers recovery: set the stop, else close the exposure and verify;
  * we never reverse or hedge to "fix" an execution problem.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .gateways import Gateway
from .models import (
    ACTION_DEAL, ACTION_SLTP, AMBIGUOUS_RETCODES, DEAL_ENTRY_IN, DEAL_ENTRY_OUT, FILL_FOK, FILL_IOC, FILL_RETURN,
    OK_RETCODES, ORDER_BUY, ORDER_SELL, PRICE_RETRY_RETCODES, RET_DONE, RET_DONE_PARTIAL, RET_INVALID_FILL,
    RET_NO_CHANGES, SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC, SYMBOL_TRADE_MODE_FULL, TIME_GTC, AccountInfo,
    ExecResult, OrderResult, Position, SymbolSpec, round_price, side_type,
)
from .monitoring_and_alerts import Alerts
from .mt5_connection import Broker
from .risk_manager import RiskInputs, check_entry, loss_at_stop, size_position
from .strategy_engine import Analysis
from .trade_journal import Journal, now_iso

log = logging.getLogger("exec")


@dataclass
class ExecContext:
    gateway: Gateway
    analysis: Analysis
    spec: SymbolSpec
    inst_cfg: dict[str, Any]
    cfg: dict[str, Any]
    authorized: bool
    expected_login: int
    risk_inputs: Callable[[AccountInfo], RiskInputs]


def filling_candidates(spec: SymbolSpec) -> list[int]:
    out: list[int] = []
    if spec.filling_mode & SYMBOL_FILLING_IOC:
        out.append(FILL_IOC)
    if spec.filling_mode & SYMBOL_FILLING_FOK:
        out.append(FILL_FOK)
    out.append(FILL_RETURN)  # broker default for market-execution symbols that expose neither flag
    return out


class OrderExecutor:
    def __init__(self, broker: Broker, journal: Journal, alerts: Alerts,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.b, self.j, self.alerts, self._clock, self._sleep = broker, journal, alerts, clock, sleep

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def comment_for(prefix: str, signal_id: str) -> str:
        return f"{prefix}{signal_id[:12]}"[:31]

    def _journal_critical(self, row: dict) -> bool:
        try:
            self.j.upsert_execution(row)
            return True
        except Exception as exc:
            log.error("execution record write failed: %s", exc)
            return False

    def _journal_soft(self, row: dict) -> None:
        try:
            self.j.upsert_execution(row)
        except Exception as exc:
            log.warning("execution update not stored (%s); will be repaired by reconciliation", exc)

    def _request(self, spec: SymbolSpec, side: str, volume: float, price: float, sl: float, tp: float, magic: int,
                 comment: str, deviation: int, filling: int) -> dict[str, Any]:
        return {"action": ACTION_DEAL, "symbol": spec.name, "volume": volume, "type": side_type(side),
                "price": price, "sl": sl, "tp": tp, "deviation": deviation, "magic": magic, "comment": comment,
                "type_time": TIME_GTC, "type_filling": filling}

    def _reject(self, ctx: ExecContext, reason: str, state: str = "REJECTED", persist: bool = False) -> ExecResult:
        log.info("entry rejected %s %s: %s", ctx.analysis.instrument, ctx.analysis.decision, reason)
        if persist:
            self._journal_soft({"signal_id": ctx.analysis.signal_id, "mode": ctx.gateway.mode, "state": state, "error": reason[:300]})
        return ExecResult(state, False, reason)

    # ------------------------------------------------------------------ entry
    def execute_entry(self, ctx: ExecContext) -> ExecResult:
        a, gw, spec = ctx.analysis, ctx.gateway, ctx.spec
        mode, rc = gw.mode, ctx.cfg["risk"]
        ex = ctx.cfg["execution"]
        side = a.decision
        if side not in ("BUY", "SELL"):
            return self._reject(ctx, "not an actionable signal")

        # 1. authorization
        if mode == "live" and not ctx.authorized:
            return self._reject(ctx, "live execution is not authorized")

        # 2. terminal and account
        term = self.b.terminal_info()
        acct = gw.account()
        if term is None or not term.connected:
            return self._reject(ctx, "terminal is not connected")
        if acct is None:
            return self._reject(ctx, "account information unavailable")
        if mode == "live":
            if not term.trade_allowed or not acct.trade_allowed or not acct.trade_expert:
                return self._reject(ctx, "trading is not permitted on this terminal/account")
            if ctx.expected_login and acct.login != ctx.expected_login:
                return self._reject(ctx, "connected account does not match the configured account")

        # 3. quote, spread, market status
        if spec.trade_mode != SYMBOL_TRADE_MODE_FULL:
            return self._reject(ctx, "symbol is not open for full trading")
        tick = self.b.tick(spec.name)
        if tick is None or tick.bid <= 0 or tick.ask < tick.bid:
            return self._reject(ctx, "no valid quote")
        spread_pts = (tick.ask - tick.bid) / spec.point
        max_spread = float(ctx.inst_cfg.get("max_spread_points", 0) or 0)
        if max_spread and spread_pts > max_spread:
            return self._reject(ctx, f"spread {spread_pts:.0f} pts above limit")

        # 4. freshness and duplicate protection
        if tick.time > a.expires_at:
            return self._reject(ctx, "signal expired")
        if self.j.get_execution(a.signal_id, mode) is not None:
            return self._reject(ctx, "signal already processed")

        # 5. levels from the live quote, 6. sizing from broker contract data
        buy = side == "BUY"
        entry = tick.ask if buy else tick.bid
        ref = a.ask if buy else a.bid
        slip_pts = float(ctx.inst_cfg.get("slippage_points", 0)) * float(rc.get("max_slippage_points_factor", 1.0))
        adverse = (entry - ref) if buy else (ref - entry)
        if adverse > slip_pts * spec.point:
            return self._reject(ctx, "price moved against the signal beyond allowed slippage")
        sl = round_price(entry - a.sl_distance if buy else entry + a.sl_distance, spec.tick_size, spec.digits, "down" if buy else "up")
        tp = round_price(entry + a.tp_distance if buy else entry - a.tp_distance, spec.tick_size, spec.digits, "up" if buy else "down")
        min_gap = max(spec.stops_level, 1) * spec.point
        if abs(entry - sl) <= min_gap or abs(tp - entry) <= min_gap:
            return self._reject(ctx, "stop or target inside the broker's minimum stop distance")
        sizing = size_position(spec, self.b.calc_profit, side, entry, sl, acct.equity, rc["risk_per_trade_pct"],
                               float(ctx.inst_cfg.get("commission_per_lot", 0)), float(ctx.inst_cfg.get("slippage_points", 0)),
                               float(ctx.inst_cfg.get("max_lots", 0)))
        a.sizing = sizing.to_dict()
        if not sizing.ok:
            return self._reject(ctx, sizing.reason)

        # 7. risk gates and margin
        margin = self.b.calc_margin(side, spec.name, sizing.volume, entry)
        if margin is None:
            return self._reject(ctx, "could not compute required margin")
        ri = ctx.risk_inputs(acct)
        fails = check_entry(ri, rc, a.instrument, spec.name, side, sizing.expected_loss, margin, acct, self._clock())
        if fails:
            return self._reject(ctx, "; ".join(fails))

        # 8. broker validation (try the permitted filling policies in order)
        magic = int(ex["magic"])
        comment = self.comment_for(ex["comment_prefix"], a.signal_id)
        deviation = int(slip_pts)
        chosen: Optional[dict[str, Any]] = None
        last_check = ""
        for filling in filling_candidates(spec):
            req = self._request(spec, side, sizing.volume, entry, sl, tp, magic, comment, deviation, filling)
            chk = gw.check(req)
            if chk is not None and chk.retcode in (0, RET_DONE):
                chosen = req
                break
            last_check = f"retcode {chk.retcode} {chk.comment}" if chk else f"no response ({self.b.last_error()})"
            if chk is not None and chk.retcode != RET_INVALID_FILL:
                break
        if chosen is None:
            return self._reject(ctx, f"order_check failed: {last_check}", persist=True)

        # 9. durable intent BEFORE sending
        base = {"signal_id": a.signal_id, "mode": mode, "instrument": a.instrument, "broker_symbol": spec.name, "side": side,
                "requested_volume": sizing.volume, "requested_price": entry, "sl": sl, "tp": tp,
                "detail": {"comment": comment, "filling": chosen["type_filling"], "deviation": deviation, "risk": sizing.to_dict()}}
        if not self._journal_critical({**base, "state": "INTENT"}):
            return self._reject(ctx, "could not persist order intent; refusing to send")
        sent_at = self._clock()

        # 10. send (bounded price retries; never retry an ambiguous outcome)
        req = chosen
        price_retries = int(ex.get("max_retries", 2))
        while True:
            self._journal_soft({**base, "state": "SENT"})
            res = gw.send(req)
            if res is None or res.retcode in AMBIGUOUS_RETCODES:
                return self._resolve_ambiguous(ctx, base, comment, sent_at, res.retcode if res else None)
            if res.retcode in OK_RETCODES:
                return self._after_fill(ctx, base, res, comment, sl, tp, sizing.expected_loss)
            if res.retcode in PRICE_RETRY_RETCODES and price_retries > 0:
                price_retries -= 1
                t2 = self.b.tick(spec.name)
                if t2 is None or t2.time > a.expires_at:
                    break
                p2 = t2.ask if buy else t2.bid
                if ((p2 - ref) if buy else (ref - p2)) > slip_pts * spec.point:
                    return self._finalize_reject(ctx, base, res.retcode, "price moved beyond allowed slippage on retry")
                if abs(p2 - sl) <= min_gap:
                    return self._finalize_reject(ctx, base, res.retcode, "retry price too close to stop")
                req = {**req, "price": p2}
                continue
            return self._finalize_reject(ctx, base, res.retcode, f"broker rejected: {res.retcode} {res.comment}")
        return self._finalize_reject(ctx, base, 0, "signal expired while retrying")

    def _finalize_reject(self, ctx: ExecContext, base: dict, retcode: int, reason: str) -> ExecResult:
        self._journal_soft({**base, "state": "REJECTED", "retcode": retcode or None, "error": reason[:300]})
        log.warning("order rejected %s: %s", ctx.analysis.instrument, reason)
        r = ExecResult("REJECTED", False, reason, retcode=retcode or None)
        if retcode in (10017, 10018, 10019, 10027, 10026):  # trade disabled / market closed / no money / autotrading off
            self.alerts.emit("warn", "order_rejected", f"{ctx.analysis.instrument}: {reason}", {"retcode": retcode})
        return r

    # ------------------------------------------------------------------ ambiguity
    def _resolve_ambiguous(self, ctx: ExecContext, base: dict, comment: str, sent_at: float, retcode: Optional[int]) -> ExecResult:
        """The broker may or may not have executed the order. Look it up by its unique comment; do NOT resend."""
        gw = ctx.gateway
        self._journal_soft({**base, "state": "AMBIGUOUS", "retcode": retcode, "error": "no definitive broker response"})
        for _ in range(5):
            self._sleep(1.0)
            pos, deals = gw.find_by_comment(comment, sent_at - 5)
            if pos is not None or any(d.entry == DEAL_ENTRY_IN for d in deals):
                ticket = pos.ticket if pos else next(d.position_id for d in deals if d.entry == DEAL_ENTRY_IN)
                deal = next((d for d in deals if d.entry == DEAL_ENTRY_IN), None)
                res = OrderResult(RET_DONE, order=ticket, deal=deal.ticket if deal else 0,
                                  volume=pos.volume if pos else (deal.volume if deal else 0.0),
                                  price=pos.price_open if pos else (deal.price if deal else 0.0), comment="recovered after timeout")
                self.alerts.emit("warn", "ambiguous_resolved", f"{ctx.analysis.instrument}: timed-out order had been filled", {"ticket": ticket})
                return self._after_fill(ctx, base, res, comment, base["sl"], base["tp"], base["detail"]["risk"]["expected_loss"])
        self.alerts.emit("critical", "ambiguous_order",
                         f"{ctx.analysis.instrument}: order outcome unknown after timeout. Check MT5 manually. New entries are locked.",
                         {"signal_id": ctx.analysis.signal_id, "comment": comment})
        return ExecResult("AMBIGUOUS", False, "order outcome unknown", retcode=retcode)

    # ------------------------------------------------------------------ fill verification and protection
    def _after_fill(self, ctx: ExecContext, base: dict, res: Any, comment: str, sl: float, tp: float, planned_loss: float) -> ExecResult:
        gw, spec, a = ctx.gateway, ctx.spec, ctx.analysis
        deal = gw.deal(res.deal) if res.deal else None
        ticket = deal.position_id if deal and deal.position_id else res.order
        pos: Optional[Position] = None
        for _ in range(4):
            pos = gw.position(ticket)
            if pos is None:
                p2, _d = gw.find_by_comment(comment, self._clock() - 120)
                pos = p2
            if pos is not None:
                break
            self._sleep(0.3)
        if pos is None:
            dl = gw.deals_for_position(ticket) or []
            if any(d.entry == DEAL_ENTRY_OUT for d in dl):
                self._journal_soft({**base, "state": "FILLED", "order_ticket": res.order, "deal_ticket": res.deal,
                                    "position_ticket": ticket, "error": "position already closed (stop/target/stop-out)"})
                return ExecResult("FILLED", True, "filled and already closed", ticket=ticket)
            self.alerts.emit("critical", "fill_unverified",
                             f"{a.instrument}: order reported filled but no position was found", {"ticket": ticket})
            self._journal_soft({**base, "state": "AMBIGUOUS", "order_ticket": res.order, "deal_ticket": res.deal, "error": "fill not verifiable"})
            return ExecResult("AMBIGUOUS", False, "fill not verifiable", ticket=ticket)

        fill = deal.price if deal else pos.price_open
        vol = pos.volume
        partial = res.retcode == RET_DONE_PARTIAL or vol + 1e-9 < base["requested_volume"]
        rec = {**base, "state": "FILLED", "filled_volume": vol, "fill_price": fill, "retcode": res.retcode,
               "order_ticket": res.order or None, "deal_ticket": res.deal or None, "position_ticket": pos.ticket}
        if partial:
            self.alerts.emit("info", "partial_fill", f"{a.instrument}: filled {vol} of {base['requested_volume']}", {"ticket": pos.ticket})
        # actual risk with the real fill price and the real volume
        real_loss = loss_at_stop(spec, self.b.calc_profit, pos.side, vol, fill, sl)
        allowed = base["detail"]["risk"]["risk_amount"]
        if real_loss > allowed * 1.25:
            self.alerts.emit("warn", "risk_above_plan", f"{a.instrument}: fill slipped, real risk {real_loss:.2f} vs {allowed:.2f}", {"ticket": pos.ticket})

        protected = self._stop_is_set(pos, sl, spec)
        if protected:
            self._journal_soft({**rec, "state": "PROTECTED", "protective_state": "OK"})
            return ExecResult("PROTECTED", True, "filled and protected", pos.ticket, fill, vol, "OK", res.retcode,
                              {"position": pos, "sl": pos.sl, "tp": pos.tp, "risk": real_loss, "partial": partial})
        self._journal_soft({**rec, "protective_state": "MISSING"})
        self.alerts.emit("critical", "missing_stop", f"{a.instrument} #{pos.ticket} filled WITHOUT a stop; recovering", {"ticket": pos.ticket})
        outcome = self.recover_protection(gw, spec, pos, sl, tp, ctx.cfg["execution"].get("stop_retries", 3))
        if outcome == "RECOVERED":
            fresh = gw.position(pos.ticket) or pos
            self._journal_soft({**rec, "state": "PROTECTED", "protective_state": "RECOVERED"})
            self.alerts.emit("warn", "stop_recovered", f"{a.instrument} #{pos.ticket}: stop established after recovery", {})
            return ExecResult("PROTECTED", True, "stop recovered", pos.ticket, fill, vol, "RECOVERED", res.retcode,
                              {"position": fresh, "sl": fresh.sl, "tp": fresh.tp, "risk": real_loss, "partial": partial})
        if outcome == "CLOSED":
            self._journal_soft({**rec, "state": "FAILED", "protective_state": "CLOSED_UNPROTECTED", "error": "closed: stop could not be set"})
            self.alerts.emit("critical", "closed_unprotected", f"{a.instrument} #{pos.ticket} closed because no stop could be set", {})
            return ExecResult("CLOSED_UNPROTECTED", False, "closed because protection failed", pos.ticket, fill, vol, "CLOSED_UNPROTECTED", res.retcode)
        self._journal_soft({**rec, "protective_state": "MISSING", "error": "UNPROTECTED POSITION STILL OPEN"})
        self.alerts.emit("critical", "unprotected_open", f"{a.instrument} #{pos.ticket} is OPEN WITHOUT A STOP and could not be closed. Act in MT5 now.", {"ticket": pos.ticket})
        return ExecResult("UNPROTECTED_OPEN", False, "unprotected position open", pos.ticket, fill, vol, "MISSING", res.retcode,
                          {"position": pos, "sl": 0.0, "tp": pos.tp, "risk": real_loss, "partial": partial})

    @staticmethod
    def _stop_is_set(pos: Position, planned_sl: float, spec: SymbolSpec) -> bool:
        if pos.sl <= 0:
            return False
        correct_side = pos.sl < pos.price_open if pos.side == "BUY" else pos.sl > pos.price_open
        return correct_side and abs(pos.sl - planned_sl) <= max(spec.tick_size * 3, spec.point * 3)

    def recover_protection(self, gw: Gateway, spec: SymbolSpec, pos: Position, planned_sl: float, planned_tp: float, retries: int = 3) -> str:
        """Try to set the stop; otherwise close. Returns RECOVERED | CLOSED | OPEN (could not do either)."""
        for _ in range(max(retries, 1)):
            tick = self.b.tick(spec.name)
            if tick is not None:
                gap = max(spec.stops_level, 2) * spec.point
                sl = planned_sl
                if pos.side == "BUY":
                    sl = round_price(min(sl, tick.bid - gap), spec.tick_size, spec.digits, "down")
                else:
                    sl = round_price(max(sl, tick.ask + gap), spec.tick_size, spec.digits, "up")
                if self._set_sltp(gw, pos, sl, pos.tp or planned_tp):
                    now = gw.position(pos.ticket)
                    if now is not None and now.sl > 0:
                        return "RECOVERED"
            self._sleep(0.5)
        for _ in range(max(retries, 1) + 2):
            if self.close_position(gw, pos, "protection_failed"):
                return "CLOSED"
            self._sleep(0.5)
        return "OPEN"

    # ------------------------------------------------------------------ modify / close
    def _set_sltp(self, gw: Gateway, pos: Position, sl: float, tp: float) -> bool:
        res = gw.send({"action": ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket, "sl": sl, "tp": tp})
        return res is not None and res.retcode in (RET_DONE, RET_NO_CHANGES)

    def modify_stop(self, gw: Gateway, spec: SymbolSpec, pos: Position, new_sl: float) -> Optional[float]:
        """Move the stop and VERIFY with the broker. Returns the acknowledged stop or None. Never loosens a stop."""
        if pos.sl > 0 and ((new_sl <= pos.sl) if pos.side == "BUY" else (new_sl >= pos.sl)):
            return None
        sent = self._set_sltp(gw, pos, new_sl, pos.tp)
        now = gw.position(pos.ticket)  # verify against broker state, whatever the reply said
        if now is not None and now.sl > 0 and abs(now.sl - new_sl) <= spec.tick_size * 1.01:
            return now.sl
        if sent:
            log.warning("stop change on #%s acknowledged but not visible in broker state", pos.ticket)
        return None

    def close_position(self, gw: Gateway, pos: Position, reason: str, deviation: int = 50) -> bool:
        """Close at market and verify the position is gone."""
        spec = self.b.symbol_spec(pos.symbol)
        tick = self.b.tick(pos.symbol)
        if spec is None or tick is None:
            return False
        buy = pos.side == "BUY"
        for filling in filling_candidates(spec):
            req = {"action": ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume, "type": ORDER_SELL if buy else ORDER_BUY,
                   "position": pos.ticket, "price": tick.bid if buy else tick.ask, "deviation": deviation,
                   "magic": pos.magic, "comment": f"FAIP-close-{reason}"[:31], "type_time": TIME_GTC, "type_filling": filling}
            res = gw.send(req)
            if res is not None and res.retcode in OK_RETCODES:
                break
            if res is not None and res.retcode != RET_INVALID_FILL:
                break
        self._sleep(0.2)
        still = gw.position(pos.ticket)
        ps = gw.positions()
        return ps is not None and still is None

"""The scalper engine: wires the modules together and runs four independent loops.

  commands  - polls the dashboard command queue (outbound only), validates and executes commands
  analysis  - evaluates every newly COMPLETED M1 candle, records signals, executes qualified entries
  manage    - tick-driven position management: trailing stops, paper stops/targets (never waits for a candle)
  state     - heartbeat, health, risk watchdog, reconnects, periodic reconciliation, dashboard state

Safety defaults: after any start or restart the engine is PAUSED, live is NOT armed, and queued commands are
discarded. Live orders are impossible unless ENGINE_ALLOW_LIVE=true on the VPS AND the user armed live in this
process run after a successful preview.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .config import ENGINE_VERSION, STRATEGY_VERSION, EnvSettings, default_config, effective_config, instrument_strategy
from .gateways import LiveGateway, PaperGateway, PaperLedger, Gateway
from .indicators import swing_high, swing_low
from .market_data import MarketData
from .models import (
    ACCOUNT_TRADE_MODE_REAL, SYMBOL_TRADE_MODE_FULL, TF_M1, TF_M15, TF_M5, AccountInfo, Position, SymbolSpec,
)
from .monitoring_and_alerts import Alerts, Health
from .mt5_connection import Broker
from .order_executor import ExecContext, OrderExecutor
from .remote_control import DeadMansSwitch, TelegramControl
from .position_reconciler import PositionReconciler, close_summary, position_row
from .risk_manager import RiskInputs, daily_loss_breached, position_open_risk, size_position
from .strategy_engine import Analysis, evaluate, make_signal_id
from .symbol_discovery import discover, verify_mapping
from .trade_journal import Journal, now_iso
from .trailing_stop_manager import TrailState, TrailingManager

log = logging.getLogger("engine")

MODES = ("live", "paper")


def _utc_midnight(now: Optional[datetime] = None) -> datetime:
    n = now or datetime.now(timezone.utc)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


class Engine:
    def __init__(self, env: EnvSettings, broker: Broker, journal: Journal, alerts: Alerts, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.env, self.broker, self.j, self.alerts, self._clock, self._sleep = env, broker, journal, alerts, clock, sleep
        self.health = Health()
        self.market = MarketData(broker, clock)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.started_at = now_iso()

        # ---- run-time state (always starts safe) ----
        self.connected = False
        self.analysis_running = False
        self.exec_mode = "none"  # none | paper | live
        self.paused = True
        self.live_armed = False
        self.locked = False
        self.lock_reason = ""
        self.preview: Optional[dict[str, Any]] = None
        self.reconcile_requested: dict[str, bool] = {"live": False, "paper": False}
        self.last_reconcile = 0.0
        self.last_reconnect_try = 0.0

        self.cfg, self.cfg_warnings = effective_config(None, env)
        self.cfg_version = 0
        self.specs: dict[str, SymbolSpec] = {}
        self.sym_of: dict[str, str] = {}  # instrument -> broker symbol (verified only)
        self.inst_of: dict[str, str] = {}
        self.last_bar: dict[str, int] = {}
        self.atr_cache: dict[str, float] = {}
        self.swing_cache: dict[tuple[str, str], float] = {}
        self.latest: dict[str, dict] = {}
        self.quotes: dict[str, dict] = {}
        self.last_nt_saved: dict[str, float] = {}
        self.last_analysis_at: Optional[str] = None
        self.next_eval: dict[str, float] = {}
        self.entry_times: dict[str, deque] = {m: deque(maxlen=200) for m in MODES}
        self.risk_cache: dict[str, dict[str, Any]] = {m: {} for m in MODES}
        self.day: dict[str, Any] = {"date": "", "live": 0.0, "paper": 0.0}
        self.live_pos: list[Position] = []
        self.paper_pos: list[Position] = []
        self.critical_cache: tuple[float, int] = (0.0, 0)

        magic = int(self.cfg["execution"]["magic"])
        self.live_gw = LiveGateway(broker, magic)
        self.ledger = PaperLedger(float(self.cfg["paper"]["start_balance"]))
        self.paper_gw = PaperGateway(broker, magic, self.ledger, self._paper_slip, broker.account_info)
        self.gws: dict[str, Gateway] = {"live": self.live_gw, "paper": self.paper_gw}
        self.executor = OrderExecutor(broker, journal, alerts, clock, sleep)
        self.trailing = TrailingManager(self._modify_stop, self._spec_of_symbol, self._trail_cfg, self._atr_of_symbol,
                                        self._swing_of, self._persist_trail, alerts.emit, clock)
        self.reconciler = PositionReconciler(journal, self.trailing, self.executor, alerts, self.inst_of.get, self._spec_of_symbol,
                                             self._atr_of_symbol, lambda s: instrument_strategy(self.cfg, self.inst_of.get(s, ""))["sl_atr_mult"],
                                             self._on_closed, int(self.cfg["execution"]["stop_retries"]))
        self.threads: list[threading.Thread] = []

    # ================================================================== helpers
    def _paper_slip(self, symbol: str) -> float:
        spec = self.specs.get(symbol)
        inst = self.inst_of.get(symbol)
        if not spec or not inst:
            return 0.0
        pts = float(self.cfg["instruments"][inst].get("slippage_points", 0)) * float(self.cfg["paper"].get("slippage_points_factor", 1.0))
        return 0.5 * pts * spec.point

    def _spec_of_symbol(self, symbol: str) -> Optional[SymbolSpec]:
        return self.specs.get(symbol)

    def _trail_cfg(self, symbol: str) -> dict:
        return self.cfg["instruments"].get(self.inst_of.get(symbol, ""), {}).get("trail", {})

    def _atr_of_symbol(self, symbol: str) -> Optional[float]:
        return self.atr_cache.get(symbol)

    def _swing_of(self, symbol: str, side: str) -> Optional[float]:
        return self.swing_cache.get((symbol, side))

    def _modify_stop(self, mode: str, p: Position, new_sl: float) -> Optional[float]:
        spec = self.specs.get(p.symbol)
        return self.executor.modify_stop(self.gws[mode], spec, p, new_sl) if spec else None

    def _persist_trail(self, st: TrailState, p: Position) -> None:
        inst = self.inst_of.get(p.symbol, p.symbol)
        row = position_row(st.mode, p, inst, st)
        self.j.defer(lambda: self.j.upsert_position(row))

    def trip_lock(self, reason: str, severity: str = "critical") -> None:
        with self.lock:
            if self.locked:
                return
            self.locked, self.lock_reason, self.paused = True, reason, True
        self.alerts.emit(severity, "trading_locked", f"Trading locked: {reason}", {})
        self._safe_audit("engine", "lock", {"reason": reason})

    def _safe_audit(self, actor: str, action: str, details: dict) -> None:
        try:
            self.j.audit(actor, action, details)
        except Exception as exc:
            log.warning("audit write failed: %s", exc)

    # ================================================================== configuration
    def load_config(self) -> None:
        stored = None
        try:
            stored = self.j.get_config()
            if stored is None:
                self.j.save_config(default_config())
        except Exception as exc:
            log.warning("config load failed (%s); keeping current", exc)
            return
        cfg, warns = effective_config(stored, self.env)
        with self.lock:
            self.cfg, self.cfg_warnings = cfg, warns
        self.health.warnings = warns

    def refresh_symbols(self) -> list[str]:
        """Verify each enabled instrument's mapped broker symbol; only verified symbols are traded."""
        problems: list[str] = []
        sym_of: dict[str, str] = {}
        for inst, ic in self.cfg["instruments"].items():
            if not ic.get("enabled"):
                continue
            ok, issues, spec = verify_mapping(self.broker, inst, ic["broker_symbol"])
            if ok and spec is not None:
                sym_of[inst] = ic["broker_symbol"]
                self.specs[ic["broker_symbol"]] = spec
            else:
                problems.append(f"{inst}: " + "; ".join(issues))
        with self.lock:
            self.sym_of = sym_of
            self.inst_of.clear()
            self.inst_of.update({v: k for k, v in sym_of.items()})
        self.health.set("symbols", bool(sym_of) and not problems, "; ".join(problems) or f"{len(sym_of)} verified")
        for pr in problems:
            self.alerts.emit("warn", "symbol_mapping", pr, {})
        return problems

    # ================================================================== connection
    def connect(self) -> tuple[bool, str]:
        ok, msg = self.broker.connect()
        if not ok:
            self.connected = False
            self.health.set("mt5", False, msg)
            return False, msg
        term, acct = self.broker.terminal_info(), self.broker.account_info()
        if term is None or acct is None or not term.connected:
            self.connected = False
            self.health.set("mt5", False, "terminal not connected to the broker server")
            return False, "terminal not connected to the broker server"
        if self.env.mt5_login and acct.login != self.env.mt5_login:
            self.connected = False
            self.health.set("mt5", False, "account mismatch")
            self.alerts.emit("critical", "account_mismatch", "The MT5 terminal is logged into a different account than MT5_LOGIN", {})
            return False, "terminal account does not match MT5_LOGIN"
        self.connected = True
        self.health.set("mt5", True, f"{acct.type_name} account {acct.login} @ {acct.server}")
        self.health.set("trading_permission", term.trade_allowed and acct.trade_allowed and acct.trade_expert,
                        "" if (term.trade_allowed and acct.trade_allowed and acct.trade_expert) else "algo trading or account trading is disabled")
        self.load_config()
        self.refresh_symbols()
        self._load_paper_state()
        self._load_risk_day()
        for m in MODES:
            self.reconcile_requested[m] = True
        return True, f"connected to {acct.type_name} account {acct.login}"

    def _load_paper_state(self) -> None:
        try:
            rows = self.j.positions("paper", "open")
            closed = self.j.closed_since("paper", "1970-01-01T00:00:00+00:00")
        except Exception as exc:
            log.warning("paper state unavailable: %s", exc)
            return
        with self.paper_gw._lock:
            self.ledger.start_balance = float(self.cfg["paper"]["start_balance"])
            self.ledger.realized = sum(float(r.get("pnl") or 0) for r in closed)
            for r in rows:
                t = int(r["ticket"])
                if t not in self.ledger.positions:
                    self.ledger.positions[t] = Position(t, r["broker_symbol"], r["side"], float(r["volume"]), float(r["entry_price"]),
                                                        float(r.get("sl") or 0), float(r.get("tp") or 0), float(r.get("current_price") or r["entry_price"]),
                                                        0.0, 0.0, self.paper_gw.magic, "", 0)

    def _load_risk_day(self) -> None:
        try:
            st = self.j.get_state() or {}
        except Exception:
            return
        rd = (st.get("health") or {}).get("risk_day") or {}
        if rd.get("date") == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
            self.day = {"date": rd["date"], "live": float(rd.get("live", 0)), "paper": float(rd.get("paper", 0))}

    # ================================================================== risk inputs
    def _day_start(self, mode: str, acct: AccountInfo) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day.get("date") != today:
            self.day = {"date": today, "live": 0.0, "paper": 0.0}
        if self.day[mode] <= 0:
            realized = self._risk_cache(mode, force=True)["realized"]
            self.day[mode] = max(acct.balance - realized, 0.0) or acct.equity
        return self.day[mode]

    def _risk_cache(self, mode: str, force: bool = False) -> dict[str, Any]:
        c = self.risk_cache[mode]
        if not force and c and self._clock() - c.get("ts", 0) < 10:
            return c
        midnight = _utc_midnight().isoformat()
        try:
            closed = self.j.closed_since(mode, midnight)
            open_rows = self.j.positions(mode, "open")
        except Exception as exc:
            log.warning("risk cache refresh failed: %s", exc)
            return c or {"realized": 0.0, "consecutive": 0, "last_loss_ts": 0.0, "opened_risk": 0.0, "ts": 0, "trades_hour": 0}
        realized = sum(float(r.get("pnl") or 0) for r in closed)
        consec, last_loss = 0, 0.0
        for r in reversed(closed):
            if float(r.get("pnl") or 0) < 0:
                consec += 1
                last_loss = last_loss or _ts(r.get("closed_at"))
            else:
                break
        opened_risk = sum(float(r.get("original_risk") or 0) for r in closed + open_rows if _ts(r.get("opened_at")) >= _utc_midnight().timestamp())
        hour_ago = self._clock() - 3600
        trades_hour = sum(1 for r in closed + open_rows if _ts(r.get("opened_at")) >= hour_ago)
        c = {"realized": realized, "consecutive": consec, "last_loss_ts": last_loss, "opened_risk": opened_risk, "ts": self._clock(), "trades_hour": trades_hour}
        self.risk_cache[mode] = c
        return c

    def risk_inputs(self, mode: str, acct: AccountInfo) -> RiskInputs:
        gw = self.gws[mode]
        c = self._risk_cache(mode, force=True)
        pos = gw.positions() or []
        floating = sum(p.profit + p.swap for p in pos)
        open_risk = 0.0
        for p in pos:
            spec = self.specs.get(p.symbol)
            if spec:
                open_risk += position_open_risk(spec, self.broker.calc_profit, p)
        return RiskInputs(
            equity=acct.equity, balance=acct.balance, free_margin=acct.margin_free, margin_used=acct.margin,
            day_start_equity=self._day_start(mode, acct), realized_today=c["realized"], floating_bot=floating,
            consecutive_losses=c["consecutive"], last_loss_ts=c["last_loss_ts"], trades_last_hour=c["trades_hour"],
            risk_opened_today=c["opened_risk"], open_positions=pos, open_risk=open_risk, locked=self.locked, lock_reason=self.lock_reason,
        )

    def _on_closed(self, mode: str, row: dict, upd: dict) -> None:
        self._risk_cache(mode, force=True)
        pnl = float(upd.get("pnl") or 0)
        sev = "info"
        self.alerts.emit(sev, "position_closed", f"{row.get('instrument', '')} #{row.get('ticket')} closed ({upd.get('close_reason')}) P&L {pnl:+.2f}", {"mode": mode})

    # ================================================================== analysis loop
    def _session_ok(self, inst_cfg: dict) -> bool:
        sess = inst_cfg.get("sessions_utc") or []
        if not sess:
            return True
        h = datetime.fromtimestamp(self._clock(), timezone.utc).hour
        return any(a <= h < b for a, b in sess)

    def analysis_pass(self) -> None:
        if not self.connected or not self.analysis_running:
            return
        for inst, symbol in list(self.sym_of.items()):
            try:
                self._analyse_instrument(inst, symbol)
            except Exception as exc:  # one instrument must never stop the others
                log.exception("analysis error for %s: %s", inst, type(exc).__name__)
                self.alerts.emit("warn", "analysis_error", f"{inst}: analysis error {type(exc).__name__}", {})

    def _analyse_instrument(self, inst: str, symbol: str) -> None:
        strat = instrument_strategy(self.cfg, inst)
        ic = self.cfg["instruments"][inst]
        spec = self.specs.get(symbol)
        tick = self.market.tick(symbol)
        if spec is None or tick is None:
            self.health.set(f"data_{inst}", False, "no quote")
            return
        self.quotes[inst] = {"symbol": symbol, "bid": tick.bid, "ask": tick.ask,
                             "spread_points": round((tick.ask - tick.bid) / spec.point, 1), "tick_time": tick.time,
                             "age_sec": round(self.market.tick_age(symbol), 1)}
        m1 = self.market.completed_bars(symbol, TF_M1, strat["bars_m1"])
        if m1 is None:
            self.health.set(f"data_{inst}", False, self.market.last_error.get(symbol, "no candles"))
            return
        self.next_eval[inst] = self._clock() + (60 - (tick.time % 60))
        fresh = self.market.tick_age(symbol) <= strat["max_tick_age_sec"]
        self.health.set(f"data_{inst}", fresh, "" if fresh else f"quote stale {self.market.tick_age(symbol):.0f}s (market closed?)")
        if m1[-1].time <= self.last_bar.get(inst, 0):
            return  # no new completed candle yet
        self.last_bar[inst] = m1[-1].time  # mark first: one evaluation per candle, ever
        m5 = self.market.completed_bars(symbol, TF_M5, strat["bars_m5"])
        m15 = self.market.completed_bars(symbol, TF_M15, strat["bars_m15"]) if strat.get("use_m15") else None
        if m5 is None:
            self.health.set(f"data_{inst}", False, "no M5 candles")
            return
        a = evaluate(inst, symbol, m1, m5, m15, tick, spec, strat, ic, self.market.tick_age(symbol), self.cfg["risk"]["min_net_rr"])
        self.latest[inst] = a.to_dict()
        self.last_analysis_at = now_iso()
        atr_v = a.indicators.get("atr14")
        if atr_v:
            self.atr_cache[symbol] = atr_v
        self.swing_cache[(symbol, "BUY")] = swing_low(m1, strat["swing_lookback"])
        self.swing_cache[(symbol, "SELL")] = swing_high(m1, strat["swing_lookback"])
        self._record_and_maybe_execute(a, ic, spec)

    def _record_and_maybe_execute(self, a: Analysis, ic: dict, spec: SymbolSpec) -> None:
        actionable = a.decision in ("BUY", "SELL")
        if not actionable:
            if not a.signal_id:
                a.signal_id = make_signal_id(a.instrument, a.bar_time, a.strategy_version, "NO TRADE")
            if self._clock() - self.last_nt_saved.get(a.instrument, 0) < 300:
                return
            self.last_nt_saved[a.instrument] = self._clock()
        row = {"signal_id": a.signal_id, "instrument": a.instrument, "broker_symbol": a.broker_symbol,
               "bar_time": datetime.fromtimestamp(a.bar_time, timezone.utc).isoformat(), "decision": a.decision,
               "outcome": "analysis", "reject_reason": "; ".join(a.reject_reasons)[:300] or None, "analysis": a.to_dict()}
        try:
            if not self.j.insert_signal(row):
                return  # this candle/entry was already processed (e.g. before a restart)
        except Exception as exc:
            log.error("signal not stored (%s); skipping the entry (no durable record, no trade)", exc)
            return
        if not actionable:
            return
        mode = self.exec_mode
        if mode == "none":
            return
        why = self._entry_blocked(mode, ic)
        if why:
            self._signal_outcome(a, "rejected", why)
            return
        acct_gw = self.gws[mode]
        ctx = ExecContext(acct_gw, a, spec, ic, self.cfg, authorized=(mode == "paper") or (self.live_armed and self.env.allow_live),
                          expected_login=self.env.mt5_login, risk_inputs=lambda acct, m=mode: self.risk_inputs(m, acct))
        res = self.executor.execute_entry(ctx)
        self.latest[a.instrument]["sizing"] = a.sizing
        if res.ok:
            self._signal_outcome(a, "paper_executed" if mode == "paper" else "live_executed", None)
            self._register_new_position(mode, a, res)
            self.entry_times[mode].append(self._clock())
            self._risk_cache(mode, force=True)
            self.alerts.emit("info", "entry", f"{mode.upper()} {a.decision} {a.instrument} {res.filled_volume} @ {res.fill_price} (SL {res.detail.get('sl')})", {})
        elif res.state in ("AMBIGUOUS", "CLOSED_UNPROTECTED", "UNPROTECTED_OPEN"):
            self._signal_outcome(a, "failed", res.reason)
            self.trip_lock(f"{res.state.lower().replace('_', ' ')} on {a.instrument}: {res.reason}")
            if res.state == "UNPROTECTED_OPEN":
                self.reconcile_requested[mode] = True
        else:
            self._signal_outcome(a, "failed" if res.state == "FAILED" else "rejected", res.reason)

    def _signal_outcome(self, a: Analysis, outcome: str, reason: Optional[str]) -> None:
        try:
            self.j.update_signal(a.signal_id, outcome, reason)
        except Exception as exc:
            log.warning("signal outcome not stored: %s", exc)

    def _entry_blocked(self, mode: str, ic: dict) -> Optional[str]:
        if self.locked:
            return f"trading locked: {self.lock_reason}"
        if self.paused:
            return "entries are paused"
        if mode == "live" and not (self.live_armed and self.env.allow_live):
            return "live is not armed"
        if not self._session_ok(ic):
            return "outside the instrument's trading session"
        crit = self._critical_count()
        if crit:
            return f"{crit} unresolved critical event(s)"
        return None

    def _critical_count(self) -> int:
        t, n = self.critical_cache
        if self._clock() - t < 20:
            return n
        try:
            n = len(self.j.unresolved_critical())
        except Exception:
            n = self.critical_cache[1]
        self.critical_cache = (self._clock(), n)
        return n

    def _register_new_position(self, mode: str, a: Analysis, res: Any) -> None:
        p: Position = res.detail["position"]
        atr_v = float(a.indicators.get("atr14") or 0)
        st = self.trailing.register(mode, p, atr_v, original_sl=res.detail["sl"], original_risk=float(res.detail["risk"]))
        row = position_row(mode, p, a.instrument, st, a.signal_id,
                           {"atr_entry": atr_v, "strategy_version": STRATEGY_VERSION, "partial": res.detail.get("partial"),
                            "protective_state": res.protective_state})
        row["opened_at"] = now_iso()
        try:
            self.j.upsert_position(row)
        except Exception as exc:
            log.error("position row not stored (%s); reconciliation will adopt it", exc)
            self.reconcile_requested[mode] = True

    # ================================================================== management loop (tick driven)
    def manage_pass(self) -> None:
        if not self.connected:
            return
        for mode in MODES:
            gw = self.gws[mode]
            positions = gw.positions()
            if positions is None:
                self.health.set(f"positions_{mode}", False, "cannot read positions")
                continue
            self.health.set(f"positions_{mode}", True, "")
            ticks: dict[str, Any] = {}
            for p in positions:
                if p.symbol not in ticks:
                    ticks[p.symbol] = self.market.tick(p.symbol)
            if mode == "paper":
                closed = False
                for sym, t in ticks.items():
                    if t is not None and self.paper_gw.on_tick(sym, t.bid, t.ask, t.time):
                        closed = True
                if closed:
                    self.reconcile_requested["paper"] = True
                positions = gw.positions() or []
            known = {p.ticket for p in positions}
            prev = {p.ticket for p in (self.live_pos if mode == "live" else self.paper_pos)}
            if prev - known:
                self.reconcile_requested[mode] = True  # something closed: complete its record
            for p in positions:
                if (mode, p.ticket) not in self.trailing.states:
                    self.reconcile_requested[mode] = True  # unknown to the trailing manager: adopt on next reconcile
                    continue
                t = ticks.get(p.symbol)
                if t is not None:
                    self.trailing.on_tick(mode, p, t)
            if mode == "live":
                self.live_pos = positions
            else:
                self.paper_pos = positions

    # ================================================================== state loop
    def state_pass(self) -> None:
        now = self._clock()
        if self.connected:
            term = self.broker.terminal_info()
            if term is None or not term.connected:
                self.connected = False
                self.health.set("mt5", False, "terminal disconnected")
                self.alerts.emit("critical", "mt5_disconnected",
                                 "MT5 terminal disconnected. Server-side broker stops still protect open trades; the engine cannot trail them until it reconnects.", {})
        if not self.connected and now - self.last_reconnect_try > 10:
            self.last_reconnect_try = now
            ok, msg = self.connect()
            if ok:
                self.alerts.emit("info", "mt5_reconnected", "MT5 reconnected; reconciling positions", {})
        if self.connected:
            self._risk_watchdog()
            if any(self.reconcile_requested.values()) or now - self.last_reconcile > 15:
                self.last_reconcile = now
                for m in MODES:
                    self.reconcile_requested[m] = False
                    try:
                        rep = self.reconciler.reconcile(self.gws[m])
                        self.health.set(f"reconcile_{m}", rep.readable, "" if rep.readable else "positions unreadable")
                        if rep.closed or rep.adopted:
                            self._risk_cache(m, force=True)
                    except Exception as exc:
                        log.exception("reconcile %s failed: %s", m, type(exc).__name__)
                        self.health.set(f"reconcile_{m}", False, type(exc).__name__)
        self.publish_state()

    def _risk_watchdog(self) -> None:
        rc = self.cfg["risk"]
        for mode in MODES:
            if mode == "live" and self.exec_mode != "live" and not self.live_pos:
                continue
            if mode == "paper" and self.exec_mode != "paper" and not self.paper_pos:
                continue
            gw = self.gws[mode]
            acct = gw.account()
            if acct is None:
                continue
            c = self._risk_cache(mode)
            floating = sum(p.profit + p.swap for p in (self.live_pos if mode == "live" else self.paper_pos))
            start = self._day_start(mode, acct)
            if daily_loss_breached(start, c["realized"], floating, rc["max_daily_loss_pct"]):
                self.breach(mode, f"daily loss limit reached ({rc['max_daily_loss_pct']}% of start-of-day equity)")
            elif c["consecutive"] >= rc["max_consecutive_losses"]:
                self.breach(mode, f"{c['consecutive']} consecutive losing trades")

    def breach(self, mode: str, reason: str) -> None:
        if self.locked:
            return
        self.trip_lock(reason)
        if self.cfg["risk"]["breach_policy"] == "close_all":
            self.alerts.emit("critical", "breach_close_all", f"Closing all {mode} positions: {reason}", {})
            self.close_positions(mode)

    def compute_mode(self) -> str:
        if not self.connected:
            return "DISCONNECTED"
        if self.locked:
            return "ERROR"
        healthy = self.health.ok("mt5", "trading_permission", "symbols") and not self.health.failing_prefix("positions_")
        if self.exec_mode == "live" and self.live_armed:
            if self.analysis_running and not self.paused and healthy and self.env.allow_live:
                return "LIVE ACTIVE"
            return "LIVE ARMED"
        if self.exec_mode == "paper" and self.analysis_running and not self.paused:
            return "PAPER"
        return "PAUSED"

    def publish_state(self) -> None:
        acct = self.broker.account_info() if self.connected else None
        paper_acct = self.paper_gw.account() if self.connected else None
        account: dict[str, Any] = {}
        if acct:
            account = {"login": acct.login, "server": acct.server, "type": acct.type_name, "currency": acct.currency,
                       "balance": acct.balance, "equity": acct.equity, "margin": acct.margin, "free_margin": acct.margin_free,
                       "leverage": acct.leverage, "company": acct.company, "hedging": acct.margin_mode == 2}
        if paper_acct:
            account["paper"] = {"balance": round(paper_acct.balance, 2), "equity": round(paper_acct.equity, 2)}
        account["day"] = {m: {"start_equity": round(self.day.get(m, 0.0), 2),
                              "realized": round(self.risk_cache[m].get("realized", 0.0), 2),
                              "floating": round(sum(p.profit + p.swap for p in (self.live_pos if m == "live" else self.paper_pos)), 2),
                              "consecutive_losses": self.risk_cache[m].get("consecutive", 0)} for m in MODES}
        hs = self.health.snapshot()
        hs["risk_day"] = self.day
        hs["config_version"] = self.cfg_version
        hs["next_eval"] = self.next_eval
        hs["live_allowed_on_vps"] = self.env.allow_live
        hs["strategy"] = STRATEGY_VERSION
        mode = self.compute_mode()
        row = {
            "mode": mode, "heartbeat_at": now_iso(), "mt5_connected": self.connected,
            "last_mt5_heartbeat_at": datetime.fromtimestamp(self.broker.last_ok, timezone.utc).isoformat() if getattr(self.broker, "last_ok", 0) else None,
            "trading_locked": self.locked, "lock_reason": self.lock_reason or None, "analysis_running": self.analysis_running,
            "entries_enabled": self.exec_mode != "none" and not self.paused and not self.locked, "paused": self.paused,
            "exec_mode": self.exec_mode, "account": account, "quotes": self.quotes,
            "analysis": {k: _trim(v) for k, v in self.latest.items()}, "health": hs,
            "last_analysis_at": self.last_analysis_at, "engine_version": ENGINE_VERSION, "started_at": self.started_at,
        }
        try:
            self.j.upsert_state(row)
            self.health.set("database", True, "")
        except Exception as exc:
            self.health.set("database", False, "state write failed")
            log.warning("state publish failed: %s", exc)

    # ================================================================== commands
    def command_pass(self) -> None:
        try:
            cmds = self.j.claim_commands(self.env.command_ttl_sec)
        except Exception as exc:
            self.health.set("database", False, "command poll failed")
            log.warning("command poll failed: %s", exc)
            return
        for c in cmds:
            name, payload = c["command"], c.get("payload") or {}
            try:
                status, result = self.handle_command(name, payload)
            except Exception as exc:
                log.exception("command %s crashed", name)
                status, result = "failed", {"error": f"{type(exc).__name__}"}
            try:
                self.j.finish_command(c["id"], status, result)
            except Exception as exc:
                log.error("could not store command result: %s", exc)
            self.publish_state()

    def handle_command(self, name: str, p: dict) -> tuple[str, dict]:
        fn = getattr(self, f"cmd_{name}", None)
        if fn is None:
            return "rejected", {"error": "unknown command"}
        needs_conn = name not in ("connect", "unlock_trading", "disarm_live", "pause_entries", "emergency_stop", "stop_analysis")
        if needs_conn and not self.connected:
            return "rejected", {"error": "MT5 is not connected"}
        return fn(p)

    def cmd_connect(self, p: dict) -> tuple[str, dict]:
        ok, msg = self.connect()
        return ("done" if ok else "failed"), {"message": msg}

    def cmd_discover_symbols(self, p: dict) -> tuple[str, dict]:
        found = discover(self.broker)
        return "done", {"candidates": found, "current": {k: v["broker_symbol"] for k, v in self.cfg["instruments"].items()}}

    def cmd_start_analysis(self, p: dict) -> tuple[str, dict]:
        if not self.sym_of:
            return "rejected", {"error": "no verified instruments; check the symbol mapping"}
        self.analysis_running = True
        self._safe_audit("engine", "analysis.start", {})
        return "done", {"message": "analysis running (no orders)"}

    def cmd_stop_analysis(self, p: dict) -> tuple[str, dict]:
        self.analysis_running, self.paused = False, True
        return "done", {"message": "analysis stopped; entries paused"}

    def cmd_start_paper(self, p: dict) -> tuple[str, dict]:
        if self.locked:
            return "rejected", {"error": f"trading is locked: {self.lock_reason}"}
        if not self.sym_of:
            return "rejected", {"error": "no verified instruments"}
        self.analysis_running, self.exec_mode, self.paused, self.live_armed = True, "paper", False, False
        self._safe_audit("engine", "paper.start", {})
        return "done", {"message": "paper trading started (simulated fills on real quotes)"}

    def live_checks(self) -> tuple[bool, list[dict]]:
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str = "", blocking: bool = True) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail, "blocking": blocking})

        add("VPS live switch (ENGINE_ALLOW_LIVE)", self.env.allow_live, "" if self.env.allow_live else "live orders are disabled on the VPS")
        term, acct = self.broker.terminal_info(), self.broker.account_info()
        add("Terminal connected", bool(term and term.connected), "")
        add("Algo trading enabled in terminal", bool(term and term.trade_allowed), "enable the Algo Trading button in MT5")
        if acct:
            add("Account trading allowed", acct.trade_allowed and acct.trade_expert, "")
            add("Account matches MT5_LOGIN", acct.login == self.env.mt5_login, f"connected {acct.login}")
            real = acct.trade_mode == ACCOUNT_TRADE_MODE_REAL
            add("Account type permitted", (not real) or self.env.allow_real_account,
                "REAL account needs ENGINE_ALLOW_REAL_ACCOUNT=true on the VPS" if real and not self.env.allow_real_account else acct.type_name)
        else:
            add("Account information", False, "unavailable")
        add("Trading not locked", not self.locked, self.lock_reason)
        try:
            crit = self.j.unresolved_critical()
            add("No unresolved critical events", not crit, f"{len(crit)} open")
            self.j.audit("engine", "live.preview", {})
            add("Database writable", True, "")
        except Exception as exc:
            add("Database writable", False, type(exc).__name__)
        unreadable = [m for m in MODES if self.gws[m].positions() is None]
        add("Broker positions readable", not unreadable, ",".join(unreadable))
        unprotected = [p.ticket for p in (self.live_gw.positions() or []) if p.sl <= 0]
        add("No unprotected positions", not unprotected, str(unprotected))
        fresh = 0
        for inst, sym in self.sym_of.items():
            spec = self.specs.get(sym)
            ic = self.cfg["instruments"][inst]
            t = self.market.tick(sym)
            quote_ok = t is not None and self.market.tick_age(sym) <= 20
            fresh += 1 if quote_ok else 0
            add(f"{inst} symbol {sym}", spec is not None and spec.trade_mode == SYMBOL_TRADE_MODE_FULL, "")
            add(f"{inst} quote fresh", quote_ok, "market closed or feed stale" if not quote_ok else "", blocking=False)
            if spec and t and acct:
                atr_v = self.atr_cache.get(sym) or (t.ask * 0.0005)
                sl = t.ask - 1.3 * atr_v
                sz = size_position(spec, self.broker.calc_profit, "BUY", t.ask, sl, acct.equity, self.cfg["risk"]["risk_per_trade_pct"],
                                   float(ic.get("commission_per_lot", 0)), float(ic.get("slippage_points", 0)), float(ic.get("max_lots", 0)))
                add(f"{inst} min lot fits risk limit", sz.ok, sz.reason or f"{sz.volume} lots at current ATR")
                if sz.ok and quote_ok:
                    from .order_executor import filling_candidates
                    req = {"action": 1, "symbol": sym, "volume": sz.volume, "type": 0, "price": t.ask, "sl": round(sl, spec.digits),
                           "deviation": 20, "magic": int(self.cfg["execution"]["magic"]), "comment": "FAIP-check", "type_time": 0,
                           "type_filling": filling_candidates(spec)[0]}
                    chk = self.broker.order_check(req)
                    add(f"{inst} order_check with stop-loss", bool(chk and chk.retcode in (0, 10009)), f"retcode {chk.retcode if chk else 'none'}")
        add("At least one live quote", fresh > 0, "all quotes stale; market may be closed")
        ok = all(c["ok"] for c in checks if c["blocking"])
        return ok, checks

    def cmd_request_live_preview(self, p: dict) -> tuple[str, dict]:
        ok, checks = self.live_checks()
        acct = self.broker.account_info()
        token = secrets.token_urlsafe(16)
        self.preview = {"token": token, "expires": self._clock() + 120, "login": acct.login if acct else 0, "ok": ok}
        rc = self.cfg["risk"]
        result = {
            "ok": ok, "preview_token": token if ok else None, "expires_in": 120,
            "account": {"login": acct.login, "server": acct.server, "type": acct.type_name, "currency": acct.currency,
                        "balance": acct.balance, "equity": acct.equity} if acct else None,
            "instruments": {i: self.sym_of.get(i) for i, c in self.cfg["instruments"].items() if c.get("enabled")},
            "risk": {k: rc[k] for k in ("risk_per_trade_pct", "max_daily_loss_pct", "max_aggregate_open_risk_pct", "max_total_positions",
                                       "max_consecutive_losses", "breach_policy")},
            "checks": checks,
            "warning": "Live trading places real orders and can lose money. A trailing stop managed by this engine only works while the "
                       "engine and the MT5 terminal are running; the broker-side stop-loss is your protection when they are not.",
        }
        return ("done" if ok else "rejected"), result

    def cmd_arm_live(self, p: dict) -> tuple[str, dict]:
        pv = self.preview
        if not pv or not pv.get("ok") or p.get("preview_token") != pv["token"] or self._clock() > pv["expires"]:
            self.preview = None
            return "rejected", {"error": "preview expired or invalid; request a new live preview"}
        self.preview = None
        ok, checks = self.live_checks()
        acct = self.broker.account_info()
        if not ok or acct is None or acct.login != pv["login"]:
            return "rejected", {"error": "pre-arm health check failed", "checks": checks}
        self.live_armed, self.exec_mode, self.paused = True, "live", True  # armed but paused: the user must resume explicitly
        self.analysis_running = True
        self._safe_audit("user", "live.armed", {"login": acct.login, "type": acct.type_name, "server": acct.server,
                                               "instruments": dict(self.sym_of), "risk": self.cfg["risk"]})
        self.alerts.emit("warn", "live_armed", f"LIVE trading ARMED on {acct.type_name} account {acct.login}. Entries are paused until you resume.", {})
        return "done", {"message": "live armed; tap Resume to allow entries"}

    def cmd_disarm_live(self, p: dict) -> tuple[str, dict]:
        self.live_armed, self.paused = False, True
        if self.exec_mode == "live":
            self.exec_mode = "none"
        self._safe_audit("user", "live.disarmed", {})
        return "done", {"message": "live disarmed; open trades keep being managed"}

    def cmd_pause_entries(self, p: dict) -> tuple[str, dict]:
        self.paused = True
        self._safe_audit("user", "entries.paused", {})
        return "done", {"message": "new entries paused; open trades keep being managed"}

    def cmd_resume_entries(self, p: dict) -> tuple[str, dict]:
        if self.locked:
            return "rejected", {"error": f"trading is locked: {self.lock_reason}"}
        if self.exec_mode == "none":
            return "rejected", {"error": "start paper trading or arm live first"}
        if self.exec_mode == "live":
            ok, checks = self.live_checks()
            if not ok or not self.live_armed:
                return "rejected", {"error": "live health check failed", "checks": [c for c in checks if c["blocking"] and not c["ok"]]}
        self.analysis_running, self.paused = True, False
        self._safe_audit("user", "entries.resumed", {"mode": self.exec_mode})
        return "done", {"message": f"{self.exec_mode} entries resumed"}

    def cmd_emergency_stop(self, p: dict) -> tuple[str, dict]:
        self.paused, self.live_armed = True, False
        self.exec_mode = "none"
        # The engine places only market orders, so there are no pending orders to cancel. Open positions are NOT closed:
        # they keep their broker stops and the trailing manager keeps protecting them.
        self._safe_audit("user", "emergency_stop", {})
        self.alerts.emit("warn", "emergency_stop", "EMERGENCY STOP: entries disabled and live disarmed. Open trades still managed.", {})
        return "done", {"message": "entries disabled, live disarmed; open trades remain protected and managed"}

    def close_positions(self, which: str = "all") -> dict[str, Any]:
        out = {"closed": [], "failed": []}
        for m in (MODES if which == "all" else (which,)):
            gw = self.gws[m]
            for pos in gw.positions() or []:
                ok = self.executor.close_position(gw, pos, "manual")
                (out["closed"] if ok else out["failed"]).append(pos.ticket)
            self.reconcile_requested[m] = True
        if out["failed"]:
            self.alerts.emit("critical", "close_failed", f"Could not close position(s) {out['failed']}. Check MT5.", {})
        return out

    def cmd_close_positions(self, p: dict) -> tuple[str, dict]:
        which = p.get("mode", "all")
        if which not in ("all", "live", "paper"):
            return "rejected", {"error": "mode must be all, live or paper"}
        res = self.close_positions(which)
        self._safe_audit("user", "positions.closed", {"scope": which, **res})
        return ("done" if not res["failed"] else "failed"), res

    def cmd_unlock_trading(self, p: dict) -> tuple[str, dict]:
        if self.connected:
            bad = [pp.ticket for pp in (self.live_gw.positions() or []) if pp.sl <= 0]
            if bad:
                return "rejected", {"error": f"unprotected position(s) still open: {bad}"}
        self.locked, self.lock_reason, self.paused = False, "", True
        try:
            self.j.resolve_risk_events()
        except Exception:
            pass
        self.critical_cache = (0.0, 0)
        self._safe_audit("user", "unlock", {})
        return "done", {"message": "unlocked; entries remain paused until you resume"}

    def cmd_reconcile(self, p: dict) -> tuple[str, dict]:
        for m in MODES:
            self.reconcile_requested[m] = True
        self.last_reconcile = 0
        return "done", {"message": "reconciliation scheduled"}

    # ================================================================== lifecycle
    def startup(self) -> None:
        n = 0
        try:
            n = self.j.expire_pending_commands()
        except Exception as exc:
            log.warning("could not expire stale commands: %s", exc)
        self._safe_audit("engine", "engine.start", {"version": ENGINE_VERSION, "discarded_commands": n, "mode": "PAUSED (safe start)"})
        self.connect()
        self.publish_state()

    def _loop(self, name: str, fn: Callable[[], None], period: float) -> None:
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                fn()
            except Exception as exc:
                log.exception("%s loop error: %s", name, type(exc).__name__)
                self.alerts.emit("warn", f"{name}_loop_error", f"{name} loop error: {type(exc).__name__}", {})
            self.stop_event.wait(max(0.05, period - (time.time() - t0)))

    def run(self) -> None:
        self.startup()
        loops = [("commands", self.command_pass, self.env.command_poll_sec), ("analysis", self.analysis_pass, 1.0),
                 ("manage", self.manage_pass, 0.25), ("state", self.state_pass, self.env.heartbeat_sec)]
        extra: list[tuple[str, Callable[[threading.Event], None]]] = []
        self.threads = [threading.Thread(target=self._loop, args=(n, f, per), name=n, daemon=True) for n, f, per in loops]
        for t in self.threads:
            t.start()
        if self.env.telegram_token and self.env.telegram_chat_id and self.env.telegram_control:
            extra.append(("telegram", TelegramControl(self, self.env.telegram_token, self.env.telegram_chat_id).run))
        if self.env.ping_url:
            extra.append(("deadman", DeadMansSwitch(self, self.env.ping_url).run))
        for n, fn in extra:
            t = threading.Thread(target=fn, args=(self.stop_event,), name=n, daemon=True)
            t.start()  # not in self.threads: a stuck Telegram poll must not make /health report the trading loops as dead
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(1.0)
        finally:
            self.stop_event.set()
            self.paused = True
            self.publish_state()

    def stop(self) -> None:
        self.stop_event.set()


def _ts(s: Optional[str]) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _trim(a: dict) -> dict:
    keep = ("instrument", "broker_symbol", "decision", "signal_id", "created_at", "bar_time", "bar_time_iso", "bid", "ask", "spread_points",
            "trend", "momentum", "indicators", "sl_price", "tp_price", "rr_gross", "rr_net", "costs", "sizing", "rationale",
            "reject_reasons", "expires_at", "invalidation", "strategy_version")
    return {k: a.get(k) for k in keep}

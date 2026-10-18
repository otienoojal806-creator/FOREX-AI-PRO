"""Test doubles: a scriptable in-memory MetaTrader 5 broker and deterministic candle generators.

FakeBroker implements the same Broker interface as MT5Broker, so the executor, trailing manager, reconciler and
engine run their real code paths. Behaviour hooks let tests simulate rejections, timeouts, partial fills,
missing stops and stop-modification failures.
"""
from __future__ import annotations

import itertools
import random
from typing import Any, Callable, Optional

from scalper_engine.models import (
    ACTION_DEAL, ACTION_SLTP, DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_REASON_CLIENT, DEAL_REASON_SL, ORDER_BUY,
    RET_DONE, RET_DONE_PARTIAL, RET_NO_CHANGES, RET_POSITION_CLOSED, RET_TIMEOUT,
    AccountInfo, Bar, CheckResult, Deal, OrderResult, Position, SymbolBrief, SymbolSpec, TerminalInfo, Tick, side_name,
)
from scalper_engine.mt5_connection import Broker

T0 = 1_760_000_000  # a fixed broker-time epoch (multiple of 60)


def gold_spec(**kw: Any) -> SymbolSpec:
    d = dict(name="XAUUSD", digits=2, point=0.01, tick_size=0.01, tick_value=1.0, tick_value_profit=1.0,
             tick_value_loss=1.0, contract_size=100.0, volume_min=0.01, volume_max=50.0, volume_step=0.01,
             stops_level=20, freeze_level=5, filling_mode=3, trade_mode=4, currency_profit="USD", path="Metals\\XAUUSD")
    d.update(kw)
    return SymbolSpec(**d)


def btc_spec(**kw: Any) -> SymbolSpec:
    d = dict(name="BTCUSD", digits=2, point=0.01, tick_size=0.01, tick_value=0.01, tick_value_profit=0.01,
             tick_value_loss=0.01, contract_size=1.0, volume_min=0.01, volume_max=10.0, volume_step=0.01,
             stops_level=500, freeze_level=100, filling_mode=2, trade_mode=4, currency_profit="USD", path="Crypto\\BTCUSD")
    d.update(kw)
    return SymbolSpec(**d)


def account(**kw: Any) -> AccountInfo:
    d = dict(login=1234567, server="Broker-Demo", currency="USD", balance=10000.0, equity=10000.0, margin=0.0,
             margin_free=10000.0, leverage=100, trade_mode=0, trade_allowed=True, trade_expert=True, margin_mode=2)
    d.update(kw)
    return AccountInfo(**d)


class FakeBroker(Broker):
    """A scriptable broker. `open_script` queues behaviours for the next opening orders, e.g. 'timeout_filled'."""

    def __init__(self) -> None:
        self.specs: dict[str, SymbolSpec] = {"XAUUSD": gold_spec(), "BTCUSD": btc_spec()}
        self.ticks: dict[str, Tick] = {"XAUUSD": Tick((T0 + 3600) * 1000, 2400.00, 2400.25),
                                       "BTCUSD": Tick((T0 + 3600) * 1000, 60000.0, 60030.0)}
        self.bars: dict[tuple[str, int], list[Bar]] = {}
        self.acct = account()
        self.term = TerminalInfo(True, True, "Fake Broker", 4000)
        self.pos: dict[int, Position] = {}
        self.deal_list: list[Deal] = []
        self.sent: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.open_script: list[str] = []
        self.check_retcode = 0
        self.check_script: list[int] = []  # queued order_check retcodes, consumed first
        self.sltp_fail = 0  # number of upcoming SLTP requests to reject
        self.sltp_silent = False  # acknowledge SLTP but do not apply it
        self.close_fail = 0
        self.connected = True
        self.fail_reads = False
        self.connect_ok = True
        self._ids = itertools.count(1000)
        self.magic_filter: Optional[int] = None

    # ---- connection ----
    def connect(self) -> tuple[bool, str]:
        return (True, "ok") if self.connect_ok else (False, "init failed")

    def shutdown(self) -> None: ...
    def terminal_info(self) -> Optional[TerminalInfo]:
        return None if self.fail_reads else (self.term if self.connected else TerminalInfo(False, False))
    def account_info(self) -> Optional[AccountInfo]: return None if self.fail_reads else self.acct
    def symbols(self) -> Optional[list[SymbolBrief]]:
        return [SymbolBrief(s.name, s.path, s.name, True, s.trade_mode) for s in self.specs.values()]
    def symbol_spec(self, symbol: str) -> Optional[SymbolSpec]: return self.specs.get(symbol)
    def symbol_select(self, symbol: str) -> bool: return symbol in self.specs
    def tick(self, symbol: str) -> Optional[Tick]: return None if self.fail_reads else self.ticks.get(symbol)
    def rates(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Optional[list[Bar]]:
        b = self.bars.get((symbol, timeframe))
        return None if b is None else b[-count:]
    def last_error(self) -> str: return "fake error"

    # ---- helpers ----
    def set_quote(self, symbol: str, bid: float, ask: float, t: Optional[float] = None) -> None:
        old = self.ticks[symbol]
        self.ticks[symbol] = Tick(int((t if t is not None else old.time + 1) * 1000), bid, ask)

    def positions(self) -> Optional[list[Position]]:
        if self.fail_reads:
            return None
        return [Position(**vars(p)) for p in self.pos.values()]

    def deals_for_position(self, ticket: int) -> Optional[list[Deal]]:
        return [d for d in self.deal_list if d.position_id == ticket]

    def deal(self, ticket: int) -> Optional[Deal]: return next((d for d in self.deal_list if d.ticket == ticket), None)
    def recent_deals(self, since_epoch: float) -> Optional[list[Deal]]: return list(self.deal_list)

    def calc_profit(self, side: str, symbol: str, volume: float, p_open: float, p_close: float) -> Optional[float]:
        s = self.specs[symbol]
        move = (p_close - p_open) if side == "BUY" else (p_open - p_close)
        return move * s.contract_size * volume

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> Optional[float]:
        return price * self.specs[symbol].contract_size * volume / max(self.acct.leverage, 1)

    def order_check(self, request: dict[str, Any]) -> Optional[CheckResult]:
        self.checks.append(dict(request))
        rc = self.check_script.pop(0) if self.check_script else self.check_retcode
        return CheckResult(rc, comment="fake check")

    def _open(self, req: dict[str, Any], volume: float, with_sl: bool = True) -> OrderResult:
        side = side_name(req["type"])
        tk = self.ticks[req["symbol"]]
        px = tk.ask if side == "BUY" else tk.bid
        tid = next(self._ids)
        did = next(self._ids)
        self.pos[tid] = Position(tid, req["symbol"], side, volume, px, req.get("sl", 0.0) if with_sl else 0.0,
                                 req.get("tp", 0.0) if with_sl else 0.0, px, 0.0, 0.0, req.get("magic", 0),
                                 req.get("comment", ""), int(tk.time))
        self.deal_list.append(Deal(did, tid, tid, req["symbol"], side, DEAL_ENTRY_IN, volume, px, 0.0, 0.0, 0.0, 0.0, 0,
                                   int(tk.time), req.get("magic", 0), req.get("comment", "")))
        return OrderResult(RET_DONE, order=tid, deal=did, volume=volume, price=px)

    def close_position_at(self, ticket: int, price: float, reason: int = DEAL_REASON_SL) -> None:
        """Simulate the broker closing a position (stop hit, stop-out, manual close in the terminal)."""
        p = self.pos.pop(ticket)
        profit = self.calc_profit(p.side, p.symbol, p.volume, p.price_open, price)
        did = next(self._ids)
        self.deal_list.append(Deal(did, did, ticket, p.symbol, "SELL" if p.side == "BUY" else "BUY", DEAL_ENTRY_OUT,
                                   p.volume, price, profit, 0.0, 0.0, 0.0, reason, int(self.ticks[p.symbol].time), p.magic, p.comment))

    def order_send(self, request: dict[str, Any]) -> Optional[OrderResult]:
        self.sent.append(dict(request))
        action = request["action"]
        if action == ACTION_SLTP:
            p = self.pos.get(request["position"])
            if p is None:
                return OrderResult(RET_POSITION_CLOSED, comment="no position")
            if self.sltp_fail > 0:
                self.sltp_fail -= 1
                return OrderResult(10016, comment="invalid stops")
            if self.sltp_silent:
                return OrderResult(RET_DONE)
            if request.get("sl", p.sl) == p.sl and request.get("tp", p.tp) == p.tp:
                return OrderResult(RET_NO_CHANGES)
            p.sl, p.tp = request.get("sl", p.sl), request.get("tp", p.tp)
            return OrderResult(RET_DONE)
        if "position" in request:  # closing order
            if self.close_fail > 0:
                self.close_fail -= 1
                return OrderResult(10006, comment="rejected")
            p = self.pos.get(request["position"])
            if p is None:
                return OrderResult(RET_POSITION_CLOSED)
            tk = self.ticks[p.symbol]
            self.close_position_at(p.ticket, tk.bid if p.side == "BUY" else tk.ask, DEAL_REASON_CLIENT)
            return OrderResult(RET_DONE, order=p.ticket, deal=self.deal_list[-1].ticket, volume=p.volume)
        # opening order
        mode = self.open_script.pop(0) if self.open_script else "ok"
        if mode == "ok":
            return self._open(request, request["volume"])
        if mode == "partial":
            r = self._open(request, round(request["volume"] / 2, 2))
            return OrderResult(RET_DONE_PARTIAL, r.order, r.deal, r.volume, r.price)
        if mode == "no_stop":  # filled, but the broker dropped the stop
            return self._open(request, request["volume"], with_sl=False)
        if mode == "timeout_filled":  # reply lost, order WAS executed
            self._open(request, request["volume"])
            return OrderResult(RET_TIMEOUT, comment="timeout")
        if mode == "timeout_lost":  # reply lost, order was NOT executed
            return OrderResult(RET_TIMEOUT, comment="timeout")
        if mode == "none":  # terminal returned nothing at all
            return None
        if mode.startswith("reject:"):
            return OrderResult(int(mode.split(":")[1]), comment="scripted rejection")
        raise AssertionError(f"unknown script {mode}")


# ---------------------------------------------------------------------------------------- candles
def make_bars(n: int, start_price: float, drift: float, noise: float, t_end: int, step: int = 60,
              seed: int = 7, tick: float = 0.01) -> list[Bar]:
    """Deterministic random-walk candles with a drift. The LAST bar opens at t_end."""
    rnd = random.Random(seed)
    bars: list[Bar] = []
    price = start_price
    t0 = t_end - (n - 1) * step
    for i in range(n):
        o = price
        c = o + drift + rnd.uniform(-noise, noise)
        hi = max(o, c) + rnd.uniform(0, noise * 0.5)
        lo = min(o, c) - rnd.uniform(0, noise * 0.5)
        bars.append(Bar(t0 + i * step, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2), 100, 25))
        price = c
    return bars


def aggregate(m1: list[Bar], k: int = 5) -> list[Bar]:
    """Aggregate M1 bars into completed Mk bars (any trailing partial group is dropped)."""
    out: list[Bar] = []
    for i in range(0, len(m1) - k + 1, k):
        g = m1[i:i + k]
        out.append(Bar(g[0].time, g[0].open, max(b.high for b in g), min(b.low for b in g), g[-1].close, sum(b.tick_volume for b in g), 25))
    return out

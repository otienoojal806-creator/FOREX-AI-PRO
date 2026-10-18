"""Trading gateways: one interface, two back ends.

LiveGateway talks to the real MT5 terminal. PaperGateway simulates fills, stops and take-profits against REAL
market data from the same terminal, so paper results follow the same code path as live execution (sizing,
checks, protective-stop verification, trailing) with only the final order submission simulated.
"""
from __future__ import annotations

import itertools
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .models import (
    ACTION_DEAL, ACTION_SLTP, DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_REASON_CLIENT, DEAL_REASON_SL, DEAL_REASON_TP,
    ORDER_BUY, RET_DONE, RET_INVALID_STOPS, RET_MARKET_CLOSED, RET_NO_CHANGES, RET_POSITION_CLOSED,
    AccountInfo, CheckResult, Deal, OrderResult, Position, side_name,
)
from .mt5_connection import Broker


class Gateway(ABC):
    mode: str = "live"

    @abstractmethod
    def account(self) -> Optional[AccountInfo]: ...
    @abstractmethod
    def check(self, req: dict[str, Any]) -> Optional[CheckResult]: ...
    @abstractmethod
    def send(self, req: dict[str, Any]) -> Optional[OrderResult]: ...
    @abstractmethod
    def positions(self) -> Optional[list[Position]]:
        """Bot-managed positions. None means 'could not read', which is different from 'none open'."""
    @abstractmethod
    def deals_for_position(self, ticket: int) -> Optional[list[Deal]]: ...
    @abstractmethod
    def deal(self, ticket: int) -> Optional[Deal]: ...
    @abstractmethod
    def find_by_comment(self, comment: str, since_epoch: float) -> tuple[Optional[Position], list[Deal]]:
        """Locate the result of an ambiguous order by its unique comment: (open position, matching deals)."""

    def position(self, ticket: int) -> Optional[Position]:
        ps = self.positions()
        return next((p for p in ps or [] if p.ticket == ticket), None)


class LiveGateway(Gateway):
    mode = "live"

    def __init__(self, broker: Broker, magic: int):
        self.b, self.magic = broker, magic

    def account(self) -> Optional[AccountInfo]:
        return self.b.account_info()

    def check(self, req: dict[str, Any]) -> Optional[CheckResult]:
        return self.b.order_check(req)

    def send(self, req: dict[str, Any]) -> Optional[OrderResult]:
        return self.b.order_send(req)

    def positions(self) -> Optional[list[Position]]:
        ps = self.b.positions()
        return None if ps is None else [p for p in ps if p.magic == self.magic]

    def deals_for_position(self, ticket: int) -> Optional[list[Deal]]:
        return self.b.deals_for_position(ticket)

    def deal(self, ticket: int) -> Optional[Deal]:
        return self.b.deal(ticket)

    def find_by_comment(self, comment: str, since_epoch: float) -> tuple[Optional[Position], list[Deal]]:
        # Brokers sometimes append text to the comment (for example "[sl 2390.1]"), so match by containment.
        ps = self.b.positions() or []
        pos = next((p for p in ps if p.magic == self.magic and comment and comment in p.comment), None)
        deals = [d for d in (self.b.recent_deals(since_epoch) or []) if d.magic == self.magic and comment and comment in d.comment]
        return pos, deals


@dataclass
class PaperLedger:
    start_balance: float
    realized: float = 0.0
    positions: dict[int, Position] = field(default_factory=dict)
    deals: dict[int, list[Deal]] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(9_000_000_001))

    def next_id(self) -> int:
        return next(self._ids)


class PaperGateway(Gateway):
    mode = "paper"

    def __init__(self, broker: Broker, magic: int, ledger: PaperLedger, slippage_for: Callable[[str], float],
                 real_account: Callable[[], Optional[AccountInfo]]):
        self.b, self.magic, self.ledger = broker, magic, ledger
        self._slip_for = slippage_for  # price slippage applied against us on every simulated fill
        self._real_account = real_account
        self._lock = threading.RLock()

    # ---- account ----
    def account(self) -> Optional[AccountInfo]:
        real = self._real_account()
        if real is None:
            return None
        with self._lock:
            floating = sum(p.profit for p in self.ledger.positions.values())
            margin = 0.0
            for p in self.ledger.positions.values():
                m = self.b.calc_margin(p.side, p.symbol, p.volume, p.price_open)
                margin += m or 0.0
        bal = self.ledger.start_balance + self.ledger.realized
        eq = bal + floating
        return AccountInfo(real.login, real.server, real.currency, bal, eq, margin, eq - margin, real.leverage,
                           real.trade_mode, True, True, 2, real.company)

    # ---- orders ----
    def check(self, req: dict[str, Any]) -> Optional[CheckResult]:
        t = self.b.tick(req["symbol"])
        if t is None:
            return None
        sl, tp = req.get("sl", 0.0), req.get("tp", 0.0)
        spec = self.b.symbol_spec(req["symbol"])
        if spec is not None and req["action"] == ACTION_DEAL and "position" not in req:
            ref = t.bid if req["type"] == ORDER_BUY else t.ask
            if (sl and abs(ref - sl) < spec.min_stop_distance) or (tp and abs(ref - tp) < spec.min_stop_distance):
                return CheckResult(RET_INVALID_STOPS, comment="stops too close")
        return CheckResult(0)

    def send(self, req: dict[str, Any]) -> Optional[OrderResult]:
        with self._lock:
            sym = req["symbol"]
            t, spec = self.b.tick(sym), self.b.symbol_spec(sym)
            if t is None or spec is None:
                return None
            if req["action"] == ACTION_SLTP:
                p = self.ledger.positions.get(req["position"])
                if p is None:
                    return OrderResult(RET_POSITION_CLOSED, comment="no such position")
                ref = t.bid if p.side == "BUY" else t.ask
                sl, tp = req.get("sl", p.sl), req.get("tp", p.tp)
                if sl and abs(ref - sl) < spec.min_stop_distance:
                    return OrderResult(RET_INVALID_STOPS, comment="stop too close")
                if sl == p.sl and tp == p.tp:
                    return OrderResult(RET_NO_CHANGES)
                p.sl, p.tp = sl, tp
                return OrderResult(RET_DONE)
            slip = self._slip_for(sym)
            if "position" in req:  # close
                p = self.ledger.positions.get(req["position"])
                if p is None:
                    return OrderResult(RET_POSITION_CLOSED, comment="no such position")
                px = t.bid - slip if p.side == "BUY" else t.ask + slip
                d = self._close(p, px, DEAL_REASON_CLIENT, t.time)
                return OrderResult(RET_DONE, order=d.order, deal=d.ticket, volume=p.volume, price=px)
            buy = req["type"] == ORDER_BUY
            px = (t.ask + slip) if buy else (t.bid - slip)
            if abs(px - req["price"]) > req.get("deviation", 0) * spec.point + spec.point:
                return OrderResult(10004, comment="requote")  # price moved beyond the allowed deviation
            tid = self.ledger.next_id()
            pos = Position(tid, sym, side_name(req["type"]), req["volume"], px, req.get("sl", 0.0), req.get("tp", 0.0),
                           px, 0.0, 0.0, self.magic, req.get("comment", ""), int(t.time))
            self.ledger.positions[tid] = pos
            did = self.ledger.next_id()
            self.ledger.deals.setdefault(tid, []).append(Deal(did, tid, tid, sym, pos.side, DEAL_ENTRY_IN, req["volume"],
                                                              px, 0.0, 0.0, 0.0, 0.0, 0, int(t.time), self.magic, pos.comment))
            return OrderResult(RET_DONE, order=tid, deal=did, volume=req["volume"], price=px)

    def _close(self, p: Position, price: float, reason: int, when: float) -> Deal:
        profit = self.b.calc_profit(p.side, p.symbol, p.volume, p.price_open, price) or 0.0
        did = self.ledger.next_id()
        d = Deal(did, did, p.ticket, p.symbol, "SELL" if p.side == "BUY" else "BUY", DEAL_ENTRY_OUT, p.volume, price,
                 profit, 0.0, 0.0, 0.0, reason, int(when), self.magic, p.comment)
        self.ledger.deals.setdefault(p.ticket, []).append(d)
        self.ledger.realized += profit
        self.ledger.positions.pop(p.ticket, None)
        return d

    # ---- state ----
    def on_tick(self, symbol: str, bid: float, ask: float, when: float) -> list[Deal]:
        """Mark open paper positions to market and trigger their stops / targets. Returns deals closed."""
        closed: list[Deal] = []
        with self._lock:
            for p in list(self.ledger.positions.values()):
                if p.symbol != symbol:
                    continue
                ref = bid if p.side == "BUY" else ask
                p.price_current = ref
                prof = self.b.calc_profit(p.side, p.symbol, p.volume, p.price_open, ref)
                p.profit = prof if prof is not None else p.profit
                buy = p.side == "BUY"
                if p.sl and ((buy and bid <= p.sl) or (not buy and ask >= p.sl)):
                    closed.append(self._close(p, p.sl, DEAL_REASON_SL, when))
                elif p.tp and ((buy and bid >= p.tp) or (not buy and ask <= p.tp)):
                    closed.append(self._close(p, p.tp, DEAL_REASON_TP, when))
        return closed

    def positions(self) -> Optional[list[Position]]:
        with self._lock:
            return [Position(**vars(p)) for p in self.ledger.positions.values()]

    def deals_for_position(self, ticket: int) -> Optional[list[Deal]]:
        return list(self.ledger.deals.get(ticket, []))

    def deal(self, ticket: int) -> Optional[Deal]:
        for ds in self.ledger.deals.values():
            for d in ds:
                if d.ticket == ticket:
                    return d
        return None

    def find_by_comment(self, comment: str, since_epoch: float) -> tuple[Optional[Position], list[Deal]]:
        pos = next((p for p in self.ledger.positions.values() if p.comment == comment), None)
        deals = [d for ds in self.ledger.deals.values() for d in ds if d.comment == comment]
        return pos, deals

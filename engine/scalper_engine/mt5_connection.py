"""MetaTrader 5 connection layer.

`Broker` is the narrow interface the rest of the engine uses; `MT5Broker` implements it with the official
`MetaTrader5` package (Windows only, imported lazily so the engine and its tests run anywhere). All calls are
serialised behind one lock (the package is not documented as thread-safe), return None on failure instead of
raising, and never log credentials.
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .models import (
    AccountInfo, Bar, CheckResult, Deal, OrderResult, Position, SymbolBrief, SymbolSpec, TerminalInfo, Tick,
    ORDER_BUY, side_name,
)

log = logging.getLogger("mt5")


class Broker(ABC):
    """Everything the engine needs from a trading terminal."""

    @abstractmethod
    def connect(self) -> tuple[bool, str]: ...
    @abstractmethod
    def shutdown(self) -> None: ...
    @abstractmethod
    def terminal_info(self) -> Optional[TerminalInfo]: ...
    @abstractmethod
    def account_info(self) -> Optional[AccountInfo]: ...
    @abstractmethod
    def symbols(self) -> Optional[list[SymbolBrief]]: ...
    @abstractmethod
    def symbol_spec(self, symbol: str) -> Optional[SymbolSpec]: ...
    @abstractmethod
    def symbol_select(self, symbol: str) -> bool: ...
    @abstractmethod
    def tick(self, symbol: str) -> Optional[Tick]: ...
    @abstractmethod
    def rates(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Optional[list[Bar]]: ...
    @abstractmethod
    def positions(self) -> Optional[list[Position]]: ...
    @abstractmethod
    def deals_for_position(self, ticket: int) -> Optional[list[Deal]]: ...
    @abstractmethod
    def deal(self, ticket: int) -> Optional[Deal]: ...
    @abstractmethod
    def recent_deals(self, since_epoch: float) -> Optional[list[Deal]]: ...
    @abstractmethod
    def calc_profit(self, side: str, symbol: str, volume: float, p_open: float, p_close: float) -> Optional[float]: ...
    @abstractmethod
    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> Optional[float]: ...
    @abstractmethod
    def order_check(self, request: dict[str, Any]) -> Optional[CheckResult]: ...
    @abstractmethod
    def order_send(self, request: dict[str, Any]) -> Optional[OrderResult]: ...
    @abstractmethod
    def last_error(self) -> str: ...


class MT5Broker(Broker):
    def __init__(self, path: str = "", login: int = 0, password: str = "", server: str = "", retries: int = 3):
        self._path, self._login, self._password, self._server = path, login, password, server
        self._retries = retries
        self._lock = threading.RLock()
        self._mt5: Any = None
        self._err = ""
        self.last_ok = 0.0  # epoch of the last successful terminal call (heartbeat)

    # ---- plumbing ----
    def _mod(self) -> Any:
        if self._mt5 is None:
            import MetaTrader5 as mt5  # type: ignore  # noqa: N813  (Windows-only package)
            self._mt5 = mt5
        return self._mt5

    def _fail(self, what: str) -> None:
        try:
            self._err = f"{what}: {self._mt5.last_error()}"
        except Exception:
            self._err = what

    def last_error(self) -> str:
        return self._err

    def connect(self) -> tuple[bool, str]:
        mt5 = self._mod()
        kwargs: dict[str, Any] = {}
        if self._path:
            kwargs["path"] = self._path
        if self._login and self._password:  # optional: otherwise attach to the terminal's logged-in account
            kwargs.update(login=self._login, password=self._password, server=self._server)
        last = ""
        with self._lock:
            for attempt in range(1, self._retries + 1):
                if mt5.initialize(**kwargs):
                    self.last_ok = time.time()
                    return True, "connected"
                last = f"initialize failed: {mt5.last_error()}"
                time.sleep(min(2 ** attempt, 8))
        self._err = last
        return False, last

    def shutdown(self) -> None:
        with self._lock:
            try:
                self._mod().shutdown()
            except Exception:
                pass

    def _call(self, name: str, *a: Any, **k: Any) -> Any:
        with self._lock:
            try:
                res = getattr(self._mod(), name)(*a, **k)
            except Exception as exc:  # the terminal can raise when it is closed mid-call
                self._err = f"{name} raised {type(exc).__name__}"
                return None
            if res is None:
                self._fail(name)
            else:
                self.last_ok = time.time()
            return res

    # ---- terminal / account ----
    def terminal_info(self) -> Optional[TerminalInfo]:
        t = self._call("terminal_info")
        if t is None:
            return None
        return TerminalInfo(bool(t.connected), bool(t.trade_allowed), str(t.company), int(t.build))

    def account_info(self) -> Optional[AccountInfo]:
        a = self._call("account_info")
        if a is None:
            return None
        return AccountInfo(
            login=int(a.login), server=str(a.server), currency=str(a.currency), balance=float(a.balance),
            equity=float(a.equity), margin=float(a.margin), margin_free=float(a.margin_free), leverage=int(a.leverage),
            trade_mode=int(a.trade_mode), trade_allowed=bool(a.trade_allowed), trade_expert=bool(a.trade_expert),
            margin_mode=int(getattr(a, "margin_mode", 2)), company=str(getattr(a, "company", "")),
        )

    # ---- symbols ----
    def symbols(self) -> Optional[list[SymbolBrief]]:
        s = self._call("symbols_get")
        if s is None:
            return None
        return [SymbolBrief(str(x.name), str(x.path), str(x.description), bool(x.visible), int(x.trade_mode)) for x in s]

    def symbol_spec(self, symbol: str) -> Optional[SymbolSpec]:
        i = self._call("symbol_info", symbol)
        if i is None:
            return None
        return SymbolSpec(
            name=str(i.name), digits=int(i.digits), point=float(i.point),
            tick_size=float(i.trade_tick_size) or float(i.point), tick_value=float(i.trade_tick_value),
            tick_value_profit=float(i.trade_tick_value_profit), tick_value_loss=float(i.trade_tick_value_loss),
            contract_size=float(i.trade_contract_size), volume_min=float(i.volume_min), volume_max=float(i.volume_max),
            volume_step=float(i.volume_step), stops_level=int(i.trade_stops_level), freeze_level=int(i.trade_freeze_level),
            filling_mode=int(i.filling_mode), trade_mode=int(i.trade_mode), currency_profit=str(i.currency_profit),
            currency_base=str(i.currency_base), path=str(i.path), description=str(i.description),
        )

    def symbol_select(self, symbol: str) -> bool:
        return bool(self._call("symbol_select", symbol, True))

    def tick(self, symbol: str) -> Optional[Tick]:
        t = self._call("symbol_info_tick", symbol)
        if t is None:
            return None
        msc = int(getattr(t, "time_msc", 0)) or int(t.time) * 1000
        return Tick(msc, float(t.bid), float(t.ask), float(getattr(t, "last", 0.0)))

    def rates(self, symbol: str, timeframe: int, start_pos: int, count: int) -> Optional[list[Bar]]:
        r = self._call("copy_rates_from_pos", symbol, timeframe, start_pos, count)
        if r is None or len(r) == 0:
            return None
        return [Bar(int(x["time"]), float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]),
                    int(x["tick_volume"]), int(x["spread"])) for x in r]

    # ---- positions / deals ----
    @staticmethod
    def _pos(p: Any) -> Position:
        return Position(
            ticket=int(p.ticket), symbol=str(p.symbol), side=side_name(int(p.type)), volume=float(p.volume),
            price_open=float(p.price_open), sl=float(p.sl), tp=float(p.tp), price_current=float(p.price_current),
            profit=float(p.profit), swap=float(p.swap), magic=int(p.magic), comment=str(p.comment), time=int(p.time),
        )

    @staticmethod
    def _deal(d: Any) -> Deal:
        return Deal(
            ticket=int(d.ticket), order=int(d.order), position_id=int(d.position_id), symbol=str(d.symbol),
            side="BUY" if int(d.type) == 0 else "SELL", entry=int(d.entry), volume=float(d.volume), price=float(d.price),
            profit=float(d.profit), commission=float(d.commission), swap=float(d.swap), fee=float(getattr(d, "fee", 0.0)),
            reason=int(getattr(d, "reason", 0)), time=int(d.time), magic=int(d.magic), comment=str(d.comment),
        )

    def positions(self) -> Optional[list[Position]]:
        # positions_get returns an empty tuple when there are none and None on error: keep the distinction.
        p = self._call("positions_get")
        if p is None:
            # Some builds return None with error code 1 (RES_S_OK) for "no positions"; any other code is a real failure.
            with self._lock:
                err = self._mod().last_error()
            return [] if err and err[0] == 1 else None
        return [self._pos(x) for x in p]

    def deals_for_position(self, ticket: int) -> Optional[list[Deal]]:
        d = self._call("history_deals_get", position=ticket)
        return None if d is None else [self._deal(x) for x in d]

    def deal(self, ticket: int) -> Optional[Deal]:
        d = self._call("history_deals_get", ticket=ticket)
        return self._deal(d[0]) if d else None

    def recent_deals(self, since_epoch: float) -> Optional[list[Deal]]:
        # A wide UTC window avoids broker-server-time offset surprises; callers filter by magic/time.
        now = datetime.now(timezone.utc)
        d = self._call("history_deals_get", now - timedelta(days=3), now + timedelta(days=2))
        if d is None:
            return None
        return [x for x in (self._deal(y) for y in d)]

    # ---- calculations / orders ----
    def calc_profit(self, side: str, symbol: str, volume: float, p_open: float, p_close: float) -> Optional[float]:
        t = self._mod().ORDER_TYPE_BUY if side == "BUY" else self._mod().ORDER_TYPE_SELL
        r = self._call("order_calc_profit", t, symbol, volume, p_open, p_close)
        return None if r is None else float(r)

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> Optional[float]:
        t = self._mod().ORDER_TYPE_BUY if side == "BUY" else self._mod().ORDER_TYPE_SELL
        r = self._call("order_calc_margin", t, symbol, volume, price)
        return None if r is None else float(r)

    def order_check(self, request: dict[str, Any]) -> Optional[CheckResult]:
        r = self._call("order_check", request)
        if r is None:
            return None
        return CheckResult(int(r.retcode), float(r.margin), float(r.margin_free), str(r.comment))

    def order_send(self, request: dict[str, Any]) -> Optional[OrderResult]:
        with self._lock:
            try:
                r = self._mod().order_send(request)
            except Exception as exc:
                self._err = f"order_send raised {type(exc).__name__}"
                return None
            if r is None:
                self._fail("order_send")
                return None
            self.last_ok = time.time()
            return OrderResult(int(r.retcode), int(r.order), int(r.deal), float(r.volume), float(r.price),
                               str(r.comment), int(r.request_id))

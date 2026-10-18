"""Broker-neutral data types, MetaTrader 5 constants and rounding helpers.

Everything the engine knows about the broker flows through these types so the strategy, risk and
execution code can be tested without a MetaTrader 5 terminal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Optional

# ---- MetaTrader5 constants (stable, documented values) ----
TF_M1, TF_M5, TF_M15 = 1, 5, 15
ORDER_BUY, ORDER_SELL = 0, 1
ACTION_DEAL, ACTION_SLTP = 1, 6
FILL_FOK, FILL_IOC, FILL_RETURN = 0, 1, 2
TIME_GTC = 0
SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC = 1, 2
SYMBOL_TRADE_MODE_FULL = 4
ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2
ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_EXCHANGE, ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 0, 1, 2
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
DEAL_REASON_CLIENT, DEAL_REASON_EXPERT, DEAL_REASON_SL, DEAL_REASON_TP, DEAL_REASON_SO = 0, 3, 4, 5, 6

RET_REQUOTE = 10004
RET_REJECT = 10006
RET_PLACED = 10008
RET_DONE = 10009
RET_DONE_PARTIAL = 10010
RET_ERROR = 10011
RET_TIMEOUT = 10012
RET_INVALID_STOPS = 10016
RET_TRADE_DISABLED = 10017
RET_MARKET_CLOSED = 10018
RET_NO_MONEY = 10019
RET_PRICE_CHANGED = 10020
RET_PRICE_OFF = 10021
RET_TOO_MANY_REQUESTS = 10024
RET_NO_CHANGES = 10025
RET_INVALID_FILL = 10030
RET_CONNECTION = 10031
RET_POSITION_CLOSED = 10036

OK_RETCODES = {RET_DONE, RET_DONE_PARTIAL}
# The broker may or may not have executed the request: never resend before reconciling.
AMBIGUOUS_RETCODES = {RET_TIMEOUT, RET_CONNECTION, RET_ERROR}
# Safe to retry once with a fresh price while the signal is still valid.
PRICE_RETRY_RETCODES = {RET_REQUOTE, RET_PRICE_CHANGED, RET_PRICE_OFF}


def side_name(order_type: int) -> str:
    return "BUY" if order_type == ORDER_BUY else "SELL"


def side_type(side: str) -> int:
    return ORDER_BUY if side == "BUY" else ORDER_SELL


# ---- rounding (Decimal based so 0.07 / 0.01 never becomes 6.999999) ----
def _d(x: float) -> Decimal:
    return Decimal(repr(float(x)))


def floor_to_step(value: float, step: float) -> float:
    """Largest multiple of step that is <= value. Never rounds up."""
    if step <= 0:
        return float(value)
    q = (_d(value) / _d(step)).to_integral_value(ROUND_FLOOR)
    # tolerate float noise such as 0.06999999999999999 meaning 0.07
    if (_d(value) / _d(step) - q) > Decimal("0.999999"):
        q += 1
    return float(q * _d(step))


def round_price(price: float, tick_size: float, digits: int, mode: str = "nearest") -> float:
    """Round a price to a valid tick. mode: 'down' | 'up' | 'nearest'."""
    if tick_size <= 0:
        return round(price, digits)
    r = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_UP}[mode]
    q = (_d(price) / _d(tick_size)).to_integral_value(r)
    return round(float(q * _d(tick_size)), digits)


# ---- data types ----
@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    tick_size: float
    tick_value: float
    tick_value_profit: float
    tick_value_loss: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int  # points
    freeze_level: int  # points
    filling_mode: int  # bitmask of SYMBOL_FILLING_*
    trade_mode: int
    currency_profit: str = ""
    currency_base: str = ""
    path: str = ""
    description: str = ""

    @property
    def value_per_price_per_lot(self) -> float:
        """Account-currency value of a 1.0 price move on 1 lot (profit side)."""
        tv = self.tick_value_profit or self.tick_value
        return tv / self.tick_size if self.tick_size else 0.0

    @property
    def min_stop_distance(self) -> float:
        return self.stops_level * self.point

    @property
    def freeze_distance(self) -> float:
        return self.freeze_level * self.point

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "digits": self.digits, "point": self.point, "tick_size": self.tick_size,
            "tick_value": self.tick_value, "contract_size": self.contract_size,
            "volume_min": self.volume_min, "volume_max": self.volume_max, "volume_step": self.volume_step,
            "stops_level": self.stops_level, "freeze_level": self.freeze_level,
            "filling_mode": self.filling_mode, "trade_mode": self.trade_mode,
            "currency_profit": self.currency_profit, "path": self.path,
        }


@dataclass(frozen=True)
class SymbolBrief:
    name: str
    path: str
    description: str
    visible: bool
    trade_mode: int


@dataclass(frozen=True)
class Tick:
    time_msc: int  # broker server time, milliseconds
    bid: float
    ask: float
    last: float = 0.0

    @property
    def time(self) -> float:
        return self.time_msc / 1000.0


@dataclass(frozen=True)
class Bar:
    time: int  # bar OPEN time, broker server time (epoch seconds)
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    spread: int = 0


@dataclass(frozen=True)
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    leverage: int
    trade_mode: int  # 0 demo, 1 contest, 2 real
    trade_allowed: bool
    trade_expert: bool
    margin_mode: int = ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
    company: str = ""

    @property
    def type_name(self) -> str:
        return {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(self.trade_mode, "UNKNOWN")


@dataclass(frozen=True)
class TerminalInfo:
    connected: bool
    trade_allowed: bool
    company: str = ""
    build: int = 0


@dataclass
class Position:
    ticket: int
    symbol: str
    side: str  # BUY | SELL
    volume: float
    price_open: float
    sl: float  # 0.0 means no stop
    tp: float
    price_current: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    magic: int = 0
    comment: str = ""
    time: int = 0


@dataclass(frozen=True)
class Deal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    side: str
    entry: int  # DEAL_ENTRY_*
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    fee: float
    reason: int
    time: int
    magic: int = 0
    comment: str = ""


@dataclass(frozen=True)
class OrderResult:
    retcode: int
    order: int = 0
    deal: int = 0
    volume: float = 0.0
    price: float = 0.0
    comment: str = ""
    request_id: int = 0


@dataclass(frozen=True)
class CheckResult:
    retcode: int
    margin: float = 0.0
    margin_free: float = 0.0
    comment: str = ""


@dataclass
class Sizing:
    ok: bool
    volume: float = 0.0
    risk_amount: float = 0.0  # allowed risk (account currency)
    expected_loss: float = 0.0  # loss at SL for the chosen volume incl. costs
    loss_per_lot: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("ok", "volume", "risk_amount", "expected_loss", "loss_per_lot", "reason")}


@dataclass
class ExecResult:
    state: str  # FILLED/PROTECTED/REJECTED/AMBIGUOUS/UNFILLED/FAILED/CLOSED_UNPROTECTED
    ok: bool
    reason: str = ""
    ticket: Optional[int] = None
    fill_price: Optional[float] = None
    filled_volume: Optional[float] = None
    protective_state: str = ""
    retcode: Optional[int] = None
    detail: dict[str, Any] = field(default_factory=dict)

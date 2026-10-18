"""Validated market data: completed candles, quotes and quote freshness."""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

from .models import Bar, Tick
from .mt5_connection import Broker


def validate_bars(bars: list[Bar], step_sec: int) -> Optional[str]:
    """Return a problem description, or None when the series is sound."""
    if not bars:
        return "empty series"
    prev = None
    for b in bars:
        vals = (b.open, b.high, b.low, b.close)
        if any((not isinstance(v, (int, float))) or math.isnan(v) or math.isinf(v) or v <= 0 for v in vals):
            return "non-finite or non-positive price"
        if b.high < max(b.open, b.close) - 1e-12 or b.low > min(b.open, b.close) + 1e-12 or b.high < b.low:
            return "inconsistent OHLC"
        if prev is not None and b.time <= prev:
            return "timestamps not strictly increasing"
        prev = b.time
    return None


class MarketData:
    def __init__(self, broker: Broker, clock: Callable[[], float] = time.time):
        self.b, self._clock = broker, clock
        self._last_change: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self.last_error: dict[str, str] = {}

    def completed_bars(self, symbol: str, timeframe: int, count: int) -> Optional[list[Bar]]:
        """Completed candles only (position 0 is the forming candle and is skipped), oldest first."""
        bars = self.b.rates(symbol, timeframe, 1, count)
        if bars is None:
            self.last_error[symbol] = f"no rates ({self.b.last_error()})"
            return None
        problem = validate_bars(bars, timeframe * 60)
        if problem:
            self.last_error[symbol] = problem
            return None
        self.last_error.pop(symbol, None)
        return bars

    def tick(self, symbol: str) -> Optional[Tick]:
        t = self.b.tick(symbol)
        if t is None or t.bid <= 0 or t.ask <= 0 or t.ask < t.bid:
            return None
        now = self._clock()
        with self._lock:
            prev = self._last_change.get(symbol)
            if prev is None or prev[0] != t.time_msc:
                self._last_change[symbol] = (t.time_msc, now)
        return t

    def tick_age(self, symbol: str) -> float:
        """Seconds since the quote last CHANGED, measured on the local clock (independent of broker time zone)."""
        with self._lock:
            prev = self._last_change.get(symbol)
        return float("inf") if prev is None else max(0.0, self._clock() - prev[1])

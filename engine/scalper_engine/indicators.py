"""Pure-Python indicator maths (no numpy) so the engine installs anywhere.

All functions take plain lists of floats and return lists of the same length; the warm-up region holds
None. EMA is seeded with an SMA, RSI/ATR use Wilder smoothing (the same convention as MT5 and TradingView).
"""
from __future__ import annotations

from typing import Optional, Sequence

from .models import Bar

Series = list[Optional[float]]


def ema(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> Series:
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi_value(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def _rsi_value(avg_g: float, avg_l: float) -> float:
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(bars: Sequence[Bar]) -> list[float]:
    out: list[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> Series:
    out: Series = [None] * len(bars)
    if len(bars) <= period:
        return out
    tr = true_range(bars)
    prev = sum(tr[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, len(bars)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def last(series: Series, back: int = 0) -> Optional[float]:
    i = len(series) - 1 - back
    return series[i] if 0 <= i < len(series) else None


def swing_low(bars: Sequence[Bar], lookback: int) -> Optional[float]:
    window = bars[-lookback:]
    return min(b.low for b in window) if window else None


def swing_high(bars: Sequence[Bar], lookback: int) -> Optional[float]:
    window = bars[-lookback:]
    return max(b.high for b in window) if window else None


def slope(series: Series, bars_back: int = 3) -> Optional[float]:
    a, b = last(series), last(series, bars_back)
    return None if a is None or b is None else a - b

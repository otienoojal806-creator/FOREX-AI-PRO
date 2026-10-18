"""Rule-based M1 scalping strategy ("ema-pullback-scalp") with M5 trend confirmation.

Documented rules (BUY shown; SELL is the exact mirror image):
  Trend (M5, completed candles):   close > EMA21 > EMA50 and EMA21 rising.
  Optional context (M15):          close > EMA50.
  Entry (last completed M1 bar):   EMA9 > EMA21 > EMA50, candle bullish and closed above EMA9,
                                   body >= min_body_atr * ATR (momentum), RSI in [rsi_buy_min, rsi_buy_max],
                                   close no further than max_extension_atr * ATR above EMA21 (no chasing).
  Filters:                         spread <= max_spread, ATR >= min_atr_spread_ratio * spread,
                                   ATR <= max_atr_ratio * mean(ATR, 50) (abnormal volatility), fresh quote and
                                   candle, net reward/risk after costs >= min_net_rr.
  Stops:                           SL = farther of (sl_atr_mult * ATR) and (swing low - buffer); rejected when wider
                                   than max_sl_atr_mult * ATR. TP = tp_rr * SL distance.
Anything incomplete, conflicting or invalid returns NO TRADE with the reasons.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from .config import STRATEGY_VERSION
from .indicators import atr, ema, last, rsi, slope, swing_high, swing_low
from .models import Bar, SymbolSpec, Tick, round_price


@dataclass
class Analysis:
    instrument: str
    broker_symbol: str
    bar_time: int  # open time of the evaluated (last completed) M1 candle, broker time
    decision: str = "NO TRADE"
    signal_id: str = ""
    strategy_version: str = STRATEGY_VERSION
    created_at: str = ""
    reference_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_points: float = 0.0
    trend: dict[str, Any] = field(default_factory=dict)
    momentum: dict[str, Any] = field(default_factory=dict)
    indicators: dict[str, Any] = field(default_factory=dict)
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    sl_distance: float = 0.0
    tp_distance: float = 0.0
    rr_gross: float = 0.0
    rr_net: float = 0.0
    costs: dict[str, float] = field(default_factory=dict)
    sizing: Optional[dict[str, Any]] = None
    rationale: list[str] = field(default_factory=list)
    reject_reasons: list[str] = field(default_factory=list)
    expires_at: float = 0.0  # broker-time epoch seconds
    invalidation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["bar_time_iso"] = datetime.fromtimestamp(self.bar_time, timezone.utc).isoformat()
        return d


def make_signal_id(instrument: str, bar_time: int, version: str, direction: str) -> str:
    """Deterministic: the same candle can never produce a second identical entry."""
    raw = f"{instrument}|{bar_time}|{version}|{direction}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _num(x: Optional[float], nd: int = 5) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def evaluate(
    instrument: str,
    broker_symbol: str,
    m1: Sequence[Bar],
    m5: Sequence[Bar],
    m15: Optional[Sequence[Bar]],
    tick: Tick,
    spec: SymbolSpec,
    strat: dict[str, Any],
    inst_cfg: dict[str, Any],
    tick_age_sec: float,
    min_net_rr: float,
) -> Analysis:
    """Evaluate the latest COMPLETED M1 candle. `m1`/`m5`/`m15` must contain completed candles only."""
    a = Analysis(instrument=instrument, broker_symbol=broker_symbol, bar_time=m1[-1].time if m1 else 0)
    a.created_at = datetime.now(timezone.utc).isoformat()
    a.bid, a.ask = tick.bid, tick.ask
    spread = tick.ask - tick.bid
    a.spread_points = round(spread / spec.point, 1) if spec.point else 0.0
    reasons = a.reject_reasons

    need = max(strat["ema_slow"], strat["rsi_period"], 60) + 5
    if len(m1) < need or len(m5) < strat["ema_slow"] + 5:
        reasons.append("not enough candle history")
        return _finish(a)

    # ---- data validity ----
    if not (tick.bid > 0 and tick.ask >= tick.bid):
        reasons.append("invalid quote")
    if tick_age_sec > strat["max_tick_age_sec"]:
        reasons.append(f"stale quote ({tick_age_sec:.0f}s)")
    bar_close = m1[-1].time + 60
    if tick.time - bar_close > strat["max_bar_lag_sec"]:
        reasons.append("last M1 candle is stale relative to the quote")
    if any(b.high < b.low or b.close <= 0 for b in m1[-5:]):
        reasons.append("malformed candle data")
    if reasons:
        return _finish(a)

    closes1 = [b.close for b in m1]
    e9, e21, e50 = (ema(closes1, strat[k]) for k in ("ema_fast", "ema_mid", "ema_slow"))
    rsi1 = rsi(closes1, strat["rsi_period"])
    atr1 = atr(m1, strat["atr_period"])
    closes5 = [b.close for b in m5]
    m5_21, m5_50 = ema(closes5, strat["ema_mid"]), ema(closes5, strat["ema_slow"])

    c, o = m1[-1].close, m1[-1].open
    v9, v21, v50, vr, va = last(e9), last(e21), last(e50), last(rsi1), last(atr1)
    m5c, m5_e21, m5_e50, m5_slope = closes5[-1], last(m5_21), last(m5_50), slope(m5_21, 3)
    if None in (v9, v21, v50, vr, va, m5_e21, m5_e50, m5_slope) or not va or va <= 0:
        reasons.append("indicators incomplete")
        return _finish(a)

    atr_vals = [x for x in atr1[-50:] if x is not None]
    atr_mean = sum(atr_vals) / len(atr_vals) if atr_vals else va
    body = abs(c - o)
    a.indicators = {
        "ema9": _num(v9), "ema21": _num(v21), "ema50": _num(v50), "rsi14": _num(vr, 2), "atr14": _num(va),
        "atr_mean50": _num(atr_mean), "m5_ema21": _num(m5_e21), "m5_ema50": _num(m5_e50), "m5_close": _num(m5c),
        "close": _num(c), "spread": _num(spread),
    }
    a.reference_price = c

    # ---- trend (M5) ----
    if m5c > m5_e21 > m5_e50 and m5_slope > 0:
        m5_trend = "BULLISH"
    elif m5c < m5_e21 < m5_e50 and m5_slope < 0:
        m5_trend = "BEARISH"
    else:
        m5_trend = "NEUTRAL"
    m1_trend = "BULLISH" if v9 > v21 > v50 else "BEARISH" if v9 < v21 < v50 else "NEUTRAL"
    m15_trend = "N/A"
    if strat.get("use_m15") and m15 and len(m15) > strat["ema_slow"] + 5:
        c15 = [b.close for b in m15]
        e15 = last(ema(c15, strat["ema_slow"]))
        if e15 is not None:
            m15_trend = "BULLISH" if c15[-1] > e15 else "BEARISH"
    a.trend = {"m5": m5_trend, "m1": m1_trend, "m15": m15_trend}
    a.momentum = {"body_atr": round(body / va, 2), "rsi": round(vr, 1), "candle": "BULL" if c > o else "BEAR" if c < o else "DOJI"}

    # ---- direction ----
    if m5_trend == "NEUTRAL":
        reasons.append("no confirmed M5 trend")
        return _finish(a)
    direction = "BUY" if m5_trend == "BULLISH" else "SELL"
    if m1_trend != m5_trend:
        reasons.append(f"M1 ({m1_trend.lower()}) conflicts with M5 ({m5_trend.lower()})")
    if m15_trend not in ("N/A", m5_trend):
        reasons.append(f"M15 ({m15_trend.lower()}) conflicts with M5")
    buy = direction == "BUY"
    if (c > o) != buy or c == o:
        reasons.append("last candle does not confirm direction")
    if (buy and c <= v9) or (not buy and c >= v9):
        reasons.append("close is on the wrong side of EMA9")
    if body < strat["min_body_atr"] * va:
        reasons.append(f"weak momentum (body {body / va:.2f} ATR < {strat['min_body_atr']})")
    lo, hi = (strat["rsi_buy_min"], strat["rsi_buy_max"]) if buy else (strat["rsi_sell_min"], strat["rsi_sell_max"])
    if not lo <= vr <= hi:
        reasons.append(f"RSI {vr:.1f} outside {lo}-{hi}")
    ext = (c - v21) if buy else (v21 - c)
    if ext > strat["max_extension_atr"] * va:
        reasons.append("price is extended from EMA21 (not chasing)")

    # ---- market quality filters ----
    max_spread_pts = float(inst_cfg.get("max_spread_points", 0) or 0)
    if max_spread_pts and a.spread_points > max_spread_pts:
        reasons.append(f"spread {a.spread_points:.0f} pts > max {max_spread_pts:.0f}")
    if spread > 0 and va < strat["min_atr_spread_ratio"] * spread:
        reasons.append("volatility too low relative to spread")
    if atr_mean > 0 and va > strat["max_atr_ratio"] * atr_mean:
        reasons.append("abnormal volatility spike")

    # ---- stops, targets and cost-adjusted reward ----
    swing = (swing_low if buy else swing_high)(m1[-strat["swing_lookback"]:], strat["swing_lookback"])
    entry = tick.ask if buy else tick.bid
    atr_sl = strat["sl_atr_mult"] * va
    swing_sl = (entry - (swing - strat["swing_buffer_atr"] * va)) if buy else ((swing + strat["swing_buffer_atr"] * va) - entry)
    sl_dist = max(atr_sl, swing_sl)
    if sl_dist > strat["max_sl_atr_mult"] * va:
        reasons.append("structure stop is too wide")
    min_dist = max(spec.min_stop_distance, spec.tick_size)
    sl_dist = max(sl_dist, min_dist * 1.5)
    tp_dist = strat["tp_rr"] * sl_dist

    comm_price = (float(inst_cfg.get("commission_per_lot", 0)) / spec.value_per_price_per_lot) if spec.value_per_price_per_lot else 0.0
    slip_price = float(inst_cfg.get("slippage_points", 0)) * spec.point
    total_cost = spread + comm_price + slip_price
    net_reward = tp_dist - total_cost
    net_risk = sl_dist + comm_price + slip_price
    a.costs = {"spread": _num(spread), "commission": _num(comm_price), "slippage": _num(slip_price), "total": _num(total_cost)}
    a.sl_distance, a.tp_distance = sl_dist, tp_dist
    a.rr_gross = round(tp_dist / sl_dist, 2) if sl_dist else 0.0
    a.rr_net = round(net_reward / net_risk, 2) if net_risk > 0 else 0.0
    if a.rr_net < min_net_rr:
        reasons.append(f"net reward/risk {a.rr_net} after costs < {min_net_rr}")

    a.sl_price = round_price(entry - sl_dist if buy else entry + sl_dist, spec.tick_size, spec.digits, "down" if buy else "up")
    a.tp_price = round_price(entry + tp_dist if buy else entry - tp_dist, spec.tick_size, spec.digits, "up" if buy else "down")

    if reasons:
        return _finish(a)

    a.decision = direction
    a.signal_id = make_signal_id(instrument, a.bar_time, a.strategy_version, direction)
    a.expires_at = bar_close + strat["signal_ttl_sec"]
    a.rationale = [
        f"M5 {m5_trend.lower()} trend (close {m5c:.{spec.digits}f}, EMA21 {m5_e21:.{spec.digits}f}, EMA50 {m5_e50:.{spec.digits}f})",
        f"M1 EMA stack {m1_trend.lower()} with {a.momentum['candle'].lower()} candle body {body / va:.2f} ATR",
        f"RSI {vr:.1f} inside {lo}-{hi}; spread {a.spread_points:.0f} pts",
        f"net R:R {a.rr_net} after spread/commission/slippage",
    ]
    a.invalidation = [
        f"signal expires {strat['signal_ttl_sec']}s after the candle closed",
        "quote moves beyond the allowed slippage before the order is sent",
        "spread, volatility, margin or risk checks fail at execution time",
        "a risk limit or trading lock engages",
    ]
    return _finish(a)


def _finish(a: Analysis) -> Analysis:
    if a.decision == "NO TRADE":
        a.rationale = a.reject_reasons[:] or ["conditions incomplete"]
    return a

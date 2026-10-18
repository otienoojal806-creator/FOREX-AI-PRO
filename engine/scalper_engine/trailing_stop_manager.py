"""Continuous, tick-driven trailing-stop management.

Independent of the one-minute signal loop. The pure function `compute_new_stop` decides; `TrailingManager`
keeps the per-position high/low-water state, applies broker constraints, submits changes through a callback and
persists state so a restart never weakens protection.

Invariants (covered by unit tests):
  * a stop is only ever moved TOWARD price, never away (monotonic protection);
  * BUY stops trigger on BID, SELL stops on ASK, so all maths is bid/ask aware;
  * proposed stops respect the broker stop level and freeze level and are rounded to a valid tick;
  * the high/low-water mark only ever improves and is restored from storage after a restart.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .models import Position, SymbolSpec, Tick, round_price

log = logging.getLogger("trail")


@dataclass
class TrailState:
    mode: str
    ticket: int
    side: str
    entry: float
    original_sl: float
    original_risk: float
    atr_entry: float
    high_water: float = 0.0  # BUY: highest BID since entry
    low_water: float = 0.0  # SELL: lowest ASK since entry
    active: bool = False
    break_even_done: bool = False
    last_ack_sl: float = 0.0
    fail_count: int = 0
    last_attempt: float = 0.0
    alerted: bool = False
    dirty: bool = False
    last_persist: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


def compute_new_stop(
    side: str, entry: float, cur_sl: float, best: float, bid: float, ask: float, atr_value: float,
    swing_level: Optional[float], tcfg: dict[str, Any], spec: SymbolSpec,
) -> Optional[float]:
    """Return a strictly better stop price, or None when no safe improvement exists."""
    if atr_value <= 0 or bid <= 0 or ask <= 0:
        return None
    point = spec.point
    min_gap = max(spec.stops_level, 2) * point
    min_step = float(tcfg.get("min_step_points", 1)) * point
    mode = tcfg.get("mode", "atr")
    buy = side == "BUY"
    profit = (best - entry) if buy else (entry - best)
    cands: list[float] = []

    if tcfg.get("break_even") and profit >= float(tcfg.get("break_even_atr", 1.0)) * atr_value:
        off = float(tcfg.get("break_even_offset_points", 0)) * point
        cands.append(entry + off if buy else entry - off)

    if mode == "fixed":
        dist = float(tcfg.get("fixed_points", 0)) * point
        activate = dist
    else:
        dist = float(tcfg.get("distance_atr", 1.0)) * atr_value
        activate = float(tcfg.get("activation_atr", 1.0)) * atr_value
    if dist > 0 and profit >= activate:
        if mode == "swing" and swing_level is not None:
            buf = 0.1 * atr_value
            cands.append(swing_level - buf if buy else swing_level + buf)
        else:
            cands.append(best - dist if buy else best + dist)
    if not cands:
        return None

    if buy:
        cand = min(max(cands), bid - min_gap)  # keep the stop a legal distance below the market
        cand = round_price(cand, spec.tick_size, spec.digits, "down")
        if cur_sl > 0:
            if bid - cur_sl <= spec.freeze_distance:  # inside the freeze zone: the broker will refuse changes
                return None
            if cand < cur_sl + max(min_step, spec.tick_size):
                return None
        return cand if cand < bid else None
    cand = max(min(cands), ask + min_gap)
    cand = round_price(cand, spec.tick_size, spec.digits, "up")
    if cur_sl > 0:
        if cur_sl - ask <= spec.freeze_distance:
            return None
        if cand > cur_sl - max(min_step, spec.tick_size):
            return None
    return cand if cand > ask else None


class TrailingManager:
    def __init__(
        self,
        modify: Callable[[str, Position, float], Optional[float]],
        spec_for: Callable[[str], Optional[SymbolSpec]],
        trail_cfg_for: Callable[[str], dict[str, Any]],
        atr_for: Callable[[str], Optional[float]],
        swing_for: Callable[[str, str], Optional[float]],
        persist: Callable[[TrailState, Position], None],
        alert: Callable[[str, str, str, dict], None],
        clock: Callable[[], float] = time.time,
    ):
        self._modify, self._spec_for, self._tcfg_for = modify, spec_for, trail_cfg_for
        self._atr_for, self._swing_for, self._persist, self._alert = atr_for, swing_for, persist, alert
        self._clock = clock
        self.states: dict[tuple[str, int], TrailState] = {}

    # ---- state ----
    def register(
        self, mode: str, p: Position, atr_entry: float, original_sl: Optional[float] = None,
        original_risk: float = 0.0, high_water: float = 0.0, low_water: float = 0.0,
        active: bool = False, break_even_done: bool = False,
    ) -> TrailState:
        """Create or restore trailing state. Water marks only ever improve versus whatever was stored."""
        key = (mode, p.ticket)
        st = self.states.get(key)
        osl = original_sl if original_sl else (p.sl or 0.0)
        if st is None:
            st = TrailState(mode, p.ticket, p.side, p.price_open, osl, original_risk, atr_entry,
                            high_water=max(high_water, p.price_current or p.price_open),
                            low_water=min([x for x in (low_water, p.price_current or p.price_open) if x > 0] or [p.price_open]),
                            active=active, break_even_done=break_even_done, last_ack_sl=p.sl)
            self.states[key] = st
        else:
            st.last_ack_sl = p.sl
        return st

    def forget(self, mode: str, ticket: int) -> None:
        self.states.pop((mode, ticket), None)

    # ---- per tick ----
    def on_tick(self, mode: str, p: Position, tick: Tick) -> Optional[float]:
        """Process one price update for one open position. Returns the new acknowledged stop if one was set."""
        st = self.states.get((mode, p.ticket))
        spec = self._spec_for(p.symbol)
        if st is None or spec is None:
            return None
        buy = p.side == "BUY"
        # water marks use the price the stop triggers on; they never go backwards
        if buy and tick.bid > st.high_water:
            st.high_water, st.dirty = tick.bid, True
        if not buy and (st.low_water == 0 or tick.ask < st.low_water):
            st.low_water, st.dirty = tick.ask, True
        st.last_ack_sl = p.sl  # the broker's current stop is the source of truth
        new_sl: Optional[float] = None
        now = self._clock()
        backoff = 0.4 if st.fail_count == 0 else min(2.0 * st.fail_count, 10.0)
        if now - st.last_attempt >= backoff:
            tcfg = self._tcfg_for(p.symbol)
            atr_v = self._atr_for(p.symbol) or st.atr_entry
            swing = self._swing_for(p.symbol, p.side)
            best = st.high_water if buy else st.low_water
            cand = compute_new_stop(p.side, p.price_open, p.sl, best, tick.bid, tick.ask, atr_v, swing, tcfg, spec)
            if cand is not None and self._never_loosens(p, cand):
                st.last_attempt = now
                ack = self._modify(mode, p, cand)
                if ack is not None:
                    st.fail_count, st.alerted, new_sl = 0, False, ack
                    st.last_ack_sl, st.active, st.dirty = ack, True, True
                    protective = (ack >= p.price_open) if buy else (ack <= p.price_open)
                    if protective:
                        st.break_even_done = True
                    p.sl = ack
                else:
                    st.fail_count += 1
                    if st.fail_count >= 5 and not st.alerted:
                        st.alerted = True
                        self._alert("critical", "trail_modify_failing",
                                    f"Stop update keeps failing on {p.symbol} #{p.ticket}", {"ticket": p.ticket, "fails": st.fail_count})
        if st.dirty and now - st.last_persist >= (0.0 if new_sl else 2.0):
            st.dirty, st.last_persist = False, now
            self._persist(st, p)
        return new_sl

    @staticmethod
    def _never_loosens(p: Position, cand: float) -> bool:
        if p.sl <= 0:
            return True
        return cand > p.sl if p.side == "BUY" else cand < p.sl

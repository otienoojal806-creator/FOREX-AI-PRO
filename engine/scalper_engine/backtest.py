"""Historical backtesting and walk-forward testing for the M1 scalping strategy (standard library only).

What is reused from the live engine (so the test follows the same rules, not a re-implementation):
  * strategy_engine.evaluate        - the exact signal rules, filters and stop/target maths
  * risk_manager.size_position      - volume sized from the contract specification, rounded DOWN to the step
  * risk_manager.check_entry        - every portfolio / daily-loss / cooldown gate
  * trailing_stop_manager.compute_new_stop - the pure trailing decision (monotonic, tick rounded, stop-level aware)

Execution model (deliberately pessimistic where candles cannot tell us the truth):
  * a signal is evaluated on a COMPLETED M1 candle and filled at the NEXT candle's open;
  * BUY fills at ask (= bid + that candle's spread) plus adverse slippage, SELL fills at bid minus slippage;
  * BUY stops/targets trigger on BID, SELL stops/targets trigger on ASK (bid + spread);
  * if a candle touches both the stop and the target the STOP is assumed to have hit first;
  * stops that gap through fill at the candle open (worse than the stop) and pay slippage; targets fill at the target;
  * commission is charged per lot at close (round turn, from the instrument config);
  * the trailing stop is updated once per candle close (the live engine updates it on every tick), and a new stop
    only protects from the NEXT candle on, so a candle's own extreme never rescues the trade within that candle.

Results are an estimate under these assumptions. They do not predict live results: see docs/BACKTESTING.md.
"""
from __future__ import annotations

import argparse
import bisect
import copy
import csv
import itertools
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from .config import DEFAULT_CONFIG, default_config, deep_merge, instrument_strategy
from .indicators import atr, last, swing_high, swing_low
from .models import AccountInfo, Bar, Position, SymbolSpec, Tick, round_price
from .risk_manager import RiskInputs, check_entry, position_open_risk, size_position
from .strategy_engine import evaluate
from .trailing_stop_manager import compute_new_stop

WARMUP_BARS = 80  # candles needed before the first evaluation (the strategy requires >= 65)

# Sensible contract specs so a backtest runs without a live terminal. ALWAYS override them with your broker's
# real values (the export script writes them next to the CSV) when you want trustworthy cost and sizing numbers.
SPEC_PRESETS: dict[str, dict[str, Any]] = {
    "XAUUSD": dict(digits=2, point=0.01, tick_size=0.01, tick_value=1.0, contract_size=100.0, volume_min=0.01,
                   volume_max=50.0, volume_step=0.01, stops_level=20, freeze_level=0),
    "BTCUSD": dict(digits=2, point=0.01, tick_size=0.01, tick_value=0.01, contract_size=1.0, volume_min=0.01,
                   volume_max=10.0, volume_step=0.01, stops_level=500, freeze_level=0),
}


def make_spec(instrument: str, **over: Any) -> SymbolSpec:
    d = dict(SPEC_PRESETS.get(instrument, SPEC_PRESETS["XAUUSD"]))
    d.update({k: v for k, v in over.items() if v is not None})
    tv = d.pop("tick_value")
    return SymbolSpec(name=instrument, tick_value=tv, tick_value_profit=tv, tick_value_loss=tv, filling_mode=3,
                      trade_mode=4, currency_profit="USD", **d)


# ----------------------------------------------------------------------------------------------- data loading
def _parse_time(s: str) -> int:
    s = s.strip()
    try:
        return int(float(s))
    except ValueError:
        pass
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def load_csv(path: str) -> tuple[list[Bar], int]:
    """Read candles (time,open,high,low,close,tick_volume,spread). Returns (bars sorted by time, rows rejected).

    Malformed rows (missing values, non-positive prices, high < low) and duplicate timestamps are dropped and counted
    so bad data never silently becomes a good-looking result.
    """
    bars: dict[int, Bar] = {}
    rejected = 0
    with open(path, newline="") as fh:
        rd = csv.DictReader(fh)
        need = {"time", "open", "high", "low", "close"}
        if not rd.fieldnames or not need.issubset({f.strip().lower() for f in rd.fieldnames}):
            raise ValueError(f"{path}: header must contain {sorted(need)} (plus optional tick_volume, spread)")
        for raw in rd:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            try:
                t = _parse_time(row["time"])
                o, h, lo, c = (float(row[k]) for k in ("open", "high", "low", "close"))
                vol = int(float(row.get("tick_volume") or 0))
                sp = int(float(row.get("spread") or 0))
            except (ValueError, KeyError):
                rejected += 1
                continue
            if min(o, h, lo, c) <= 0 or h < lo or h < max(o, c) - 1e-9 or lo > min(o, c) + 1e-9 or t in bars:
                rejected += 1
                continue
            bars[t] = Bar(t, o, h, lo, c, vol, max(sp, 0))
    return [bars[t] for t in sorted(bars)], rejected


def aggregate_bars(bars: Sequence[Bar], seconds: int) -> list[Bar]:
    """Build higher-timeframe candles from a lower timeframe (only complete groups are kept)."""
    groups: dict[int, list[Bar]] = {}
    for b in bars:
        groups.setdefault(b.time - b.time % seconds, []).append(b)
    out: list[Bar] = []
    base = bars[1].time - bars[0].time if len(bars) > 1 else 60
    need = max(1, seconds // max(base, 1))
    for t in sorted(groups):
        g = groups[t]
        if len(g) < need:
            continue
        out.append(Bar(t, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                       sum(x.tick_volume for x in g), g[-1].spread))
    return out


# ----------------------------------------------------------------------------------------------- results
@dataclass
class Trade:
    side: str
    signal_id: str
    open_time: int
    close_time: int
    volume: float
    entry: float
    exit: float
    original_sl: float
    final_sl: float
    sl_history: list[float]
    risk: float  # account-currency loss if the ORIGINAL stop had been hit, including costs, at sizing time
    gross: float
    spread_cost: float
    slippage_cost: float
    commission: float
    pnl: float  # net of every cost
    reason: str  # SL | TP | TRAILING_SL | BREAKEVEN_SL | END

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["sl_history"] = list(self.sl_history)
        return d


@dataclass
class BtResult:
    instrument: str
    start_time: int
    end_time: int
    start_balance: float
    trades: list[Trade] = field(default_factory=list)
    end_balance: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    risk_events: dict[str, int] = field(default_factory=dict)
    signals: int = 0
    bars: int = 0

    def metrics(self) -> dict[str, Any]:
        m = metrics_from_trades(self.trades, self.start_balance, self.start_time, self.end_time)
        m["max_drawdown"] = round(self.max_drawdown, 2)
        m["max_drawdown_pct"] = round(self.max_drawdown_pct, 2)
        m["end_balance"] = round(self.end_balance, 2)
        m["return_pct"] = round((self.end_balance / self.start_balance - 1) * 100, 2) if self.start_balance else 0.0
        m["risk_events"] = dict(sorted(self.risk_events.items(), key=lambda kv: -kv[1]))
        m["risk_event_total"] = sum(self.risk_events.values())
        m["signals_generated"] = self.signals
        m["candles"] = self.bars
        return m


def trade_sequence_drawdown(pnls: Iterable[float], start_balance: float) -> tuple[float, float]:
    """Max drawdown (amount, percent of peak) of the balance curve built from consecutive trade results."""
    bal = peak = start_balance
    dd = ddp = 0.0
    for p in pnls:
        bal += p
        peak = max(peak, bal)
        if peak - bal > dd:
            dd = peak - bal
        if peak > 0:
            ddp = max(ddp, (peak - bal) / peak * 100)
    return dd, ddp


def metrics_from_trades(trades: Sequence[Trade], start_balance: float, start_time: int, end_time: int) -> dict[str, Any]:
    n = len(trades)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    gp, gl = sum(wins), -sum(losses)
    days = max((end_time - start_time) / 86400.0, 1e-9) if end_time > start_time else 0.0
    streak = cur = 0
    for t in trades:
        cur = cur + 1 if t.pnl < 0 else 0
        streak = max(streak, cur)
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    net = sum(t.pnl for t in trades)
    rs = [t.pnl / t.risk for t in trades if t.risk > 0]
    spread_c = sum(t.spread_cost for t in trades)
    slip_c = sum(t.slippage_cost for t in trades)
    comm_c = sum(t.commission for t in trades)
    dd, ddp = trade_sequence_drawdown([t.pnl for t in trades], start_balance)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else 0.0,
        "net_pnl": round(net, 2),
        "expectancy": round(net / n, 4) if n else 0.0,  # average net result per trade, account currency
        "expectancy_r": round(sum(rs) / len(rs), 4) if rs else 0.0,  # average result in units of the risk taken
        "profit_factor": round(gp / gl, 3) if gl > 0 else (None if gp == 0 else math.inf),
        "avg_win": round(gp / len(wins), 4) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 4) if losses else 0.0,
        "payoff_ratio": round((gp / len(wins)) / (gl / len(losses)), 3) if wins and losses and gl > 0 else None,
        "trades_per_day": round(n / days, 3) if days else 0.0,
        "days": round(days, 2),
        "longest_losing_streak": streak,
        "trade_sequence_drawdown": round(dd, 2),
        "trade_sequence_drawdown_pct": round(ddp, 2),
        "costs": {"spread": round(spread_c, 2), "slippage": round(slip_c, 2), "commission": round(comm_c, 2),
                  "total": round(spread_c + slip_c + comm_c, 2)},
        "exit_reasons": reasons,
    }


# ----------------------------------------------------------------------------------------------- simulation
@dataclass
class _Open:
    side: str
    signal_id: str
    volume: float
    entry: float
    sl: float
    tp: float
    original_sl: float
    risk: float
    open_time: int
    high_water: float
    low_water: float
    spread_cost: float
    slippage_cost: float
    sl_history: list[float] = field(default_factory=list)
    ticket: int = 0
    trailed: bool = False

    def as_position(self, symbol: str, price: float) -> Position:
        return Position(self.ticket, symbol, self.side, self.volume, self.entry, self.sl, self.tp, price_current=price)


def _calc_for(spec: SymbolSpec):
    vppl = spec.value_per_price_per_lot

    def calc(side: str, symbol: str, volume: float, open_p: float, close_p: float) -> float:
        d = (close_p - open_p) if side == "BUY" else (open_p - close_p)
        return d * volume * vppl
    return calc


def check_bar_exit(pos: _Open, b: Bar, sp: float, slip: float) -> Optional[tuple[float, str]]:
    """Decide whether a candle closes the position. Returns (fill price, reason) or None.

    Pessimistic ordering: the stop is tested before the target, and a gap through the stop fills at the open.
    """
    if pos.side == "BUY":
        o, hi, lo = b.open, b.high, b.low  # BID prices
        if pos.sl > 0:
            if o <= pos.sl:
                return o - slip, _sl_reason(pos)
            if lo <= pos.sl:
                return pos.sl - slip, _sl_reason(pos)
        if pos.tp > 0:
            if o >= pos.tp:
                return o, "TP"
            if hi >= pos.tp:
                return pos.tp, "TP"
        return None
    o, hi, lo = b.open + sp, b.high + sp, b.low + sp  # ASK prices
    if pos.sl > 0:
        if o >= pos.sl:
            return o + slip, _sl_reason(pos)
        if hi >= pos.sl:
            return pos.sl + slip, _sl_reason(pos)
    if pos.tp > 0:
        if o <= pos.tp:
            return o, "TP"
        if lo <= pos.tp:
            return pos.tp, "TP"
    return None


def _sl_reason(pos: _Open) -> str:
    if not pos.trailed:
        return "SL"
    protective = (pos.sl >= pos.entry) if pos.side == "BUY" else (pos.sl <= pos.entry)
    return "TRAILING_SL" if protective else "SL"


def _day(t: int) -> int:
    return t // 86400


def simulate(
    instrument: str, m1: Sequence[Bar], m5: Sequence[Bar], spec: SymbolSpec, cfg: Optional[dict[str, Any]] = None,
    m15: Optional[Sequence[Bar]] = None, *, start_balance: float = 10000.0, leverage: int = 100,
    default_spread_points: float = 25.0, start_time: Optional[int] = None, end_time: Optional[int] = None,
) -> BtResult:
    """Replay [start_time, end_time) of the candle history. Earlier candles are used only as indicator warm-up."""
    cfg = cfg or default_config()
    if instrument not in cfg["instruments"]:
        raise ValueError(f"unknown instrument {instrument}")
    ic = cfg["instruments"][instrument]
    strat = instrument_strategy(cfg, instrument)
    risk_cfg, tcfg = cfg["risk"], ic["trail"]
    if not m1:
        raise ValueError("no M1 candles")
    t_lo = start_time if start_time is not None else m1[0].time
    t_hi = end_time if end_time is not None else m1[-1].time + 60
    res = BtResult(instrument, t_lo, t_hi, start_balance)
    calc = _calc_for(spec)
    vppl = spec.value_per_price_per_lot
    point = spec.point
    slip = float(ic.get("slippage_points", 0)) * point  # simulated adverse slippage per fill
    comm_lot = float(ic.get("commission_per_lot", 0.0))
    max_lots = float(ic.get("max_lots", 0.0))
    sessions = ic.get("sessions_utc") or []
    m5_times = [b.time for b in m5]
    m15_times = [b.time for b in m15] if m15 else []
    win1, win5, win15 = int(strat["bars_m1"]), int(strat["bars_m5"]), int(strat["bars_m15"])
    account_tpl = AccountInfo(0, "backtest", "USD", 0, 0, 0, 0, leverage, 0, True, True, 2)

    def spread_of(b: Bar) -> float:
        pts = b.spread if b.spread > 0 else default_spread_points
        return pts * point

    balance = start_balance
    peak = start_balance
    opens: list[_Open] = []
    pending: Optional[dict[str, Any]] = None
    next_ticket = itertools.count(1)
    day_key = None
    day_start_equity = start_balance
    realized_today = 0.0
    risk_opened_today = 0.0
    consecutive = 0
    last_loss_ts = 0.0
    entry_times: list[int] = []

    def event(name: str) -> None:
        res.risk_events[name] = res.risk_events.get(name, 0) + 1

    def close(pos: _Open, price: float, t: int, reason: str) -> None:
        nonlocal balance, realized_today, consecutive, last_loss_ts
        d = (price - pos.entry) if pos.side == "BUY" else (pos.entry - price)
        gross = d * pos.volume * vppl
        comm = comm_lot * pos.volume
        slip_cost = pos.slippage_cost
        # an adverse slippage on a stop exit is already inside `price`; account it as cost, not as extra loss
        if reason in ("SL", "TRAILING_SL") and slip > 0:
            slip_cost += slip * pos.volume * vppl
        pnl = gross - comm
        balance += pnl
        realized_today += pnl
        if pnl < 0:
            consecutive += 1
            last_loss_ts = float(t)
        elif pnl > 0:
            consecutive = 0
        res.trades.append(Trade(pos.side, pos.signal_id, pos.open_time, t, pos.volume, pos.entry, price, pos.original_sl,
                                pos.sl, list(pos.sl_history), pos.risk, gross, pos.spread_cost, slip_cost, comm, pnl, reason))

    start_idx = max(WARMUP_BARS, bisect.bisect_left([b.time for b in m1], t_lo))
    n = len(m1)
    last_i = -1
    for i in range(start_idx, n):
        b = m1[i]
        if b.time >= t_hi:
            break
        res.bars += 1
        last_i = i
        t_open, t_close = b.time, b.time + 60
        sp = spread_of(b)
        dk = _day(t_open)
        if dk != day_key:  # new UTC day: daily counters reset (the engine's risk day is UTC midnight too)
            day_key = dk
            day_start_equity = balance
            realized_today = 0.0
            risk_opened_today = 0.0
            consecutive = 0  # the live engine needs a human to resume; an unattended replay would otherwise stay stopped forever
            entry_times = []

        # 1) fill the order generated by the previous candle at this candle's open
        if pending is not None:
            p, pending = pending, None
            buy = p["side"] == "BUY"
            fill = (b.open + sp + slip) if buy else (b.open - slip)
            fill = round_price(fill, spec.tick_size, spec.digits, "up" if buy else "down")
            sl = round_price(fill - p["sl_dist"] if buy else fill + p["sl_dist"], spec.tick_size, spec.digits, "down" if buy else "up")
            tp = round_price(fill + p["tp_dist"] if buy else fill - p["tp_dist"], spec.tick_size, spec.digits, "up" if buy else "down")
            pos = _Open(p["side"], p["signal_id"], p["volume"], fill, sl, tp, sl, p["risk"], t_open,
                        b.open if buy else b.open + sp, b.open + sp, sp * p["volume"] * vppl, slip * p["volume"] * vppl,
                        [sl], next(next_ticket))
            opens.append(pos)
            entry_times.append(t_open)
            risk_opened_today += p["risk"]

        # 2) walk every open position through this candle
        for pos in list(opens):
            ex = check_bar_exit(pos, b, sp, slip)
            if ex is not None:
                opens.remove(pos)
                close(pos, ex[0], t_close, ex[1])
                continue
            if pos.side == "BUY":
                pos.high_water = max(pos.high_water, b.high)
            else:
                pos.low_water = min(pos.low_water, b.low + sp)
        if opens:
            av = last(atr(m1[max(0, i - 120):i + 1], int(strat["atr_period"])))
            if av:
                bid, ask = b.close, b.close + sp
                for pos in opens:
                    buy = pos.side == "BUY"
                    sw = (swing_low if buy else swing_high)(m1[max(0, i - 12):i + 1], int(strat["swing_lookback"]))
                    cand = compute_new_stop(pos.side, pos.entry, pos.sl, pos.high_water if buy else pos.low_water,
                                            bid, ask, av, sw, tcfg, spec)
                    # never loosen: this guard mirrors TrailingManager._never_loosens
                    if cand is not None and (cand > pos.sl if buy else cand < pos.sl):
                        pos.sl = cand
                        pos.trailed = True
                        pos.sl_history.append(cand)

        # 3) mark to market for drawdown
        floating = sum(((b.close - o.entry) if o.side == "BUY" else (o.entry - (b.close + sp))) * o.volume * vppl for o in opens)
        equity = balance + floating
        peak = max(peak, equity)
        if peak - equity > res.max_drawdown:
            res.max_drawdown = peak - equity
        if peak > 0:
            res.max_drawdown_pct = max(res.max_drawdown_pct, (peak - equity) / peak * 100)

        # 4) look for a new entry on the candle that just completed (filled at the next open)
        if i + 1 >= n or m1[i + 1].time >= t_hi or pending is not None:
            continue
        if sessions:
            h = datetime.fromtimestamp(t_close, timezone.utc).hour
            if not any(a <= h < z for a, z in sessions):
                continue
        k5 = bisect.bisect_right(m5_times, t_close - 300)  # M5 candles that closed by now
        if k5 < int(strat["ema_slow"]) + 5:
            continue
        m15_win = None
        if m15 and strat.get("use_m15"):
            m15_win = m15[max(0, bisect.bisect_right(m15_times, t_close - 900) - win15):bisect.bisect_right(m15_times, t_close - 900)]
        tick = Tick(int((t_close + 1) * 1000), b.close, b.close + sp)
        a = evaluate(instrument, spec.name, m1[max(0, i + 1 - win1):i + 1], m5[max(0, k5 - win5):k5], m15_win, tick, spec,
                     strat, ic, 0.0, float(risk_cfg["min_net_rr"]))
        if a.decision not in ("BUY", "SELL"):
            continue
        res.signals += 1
        side = a.decision
        entry_ref = tick.ask if side == "BUY" else tick.bid
        eq_now = balance + floating
        sz = size_position(spec, calc, side, entry_ref, float(a.sl_price), eq_now, float(risk_cfg["risk_per_trade_pct"]),
                           comm_lot, float(ic.get("slippage_points", 0)), max_lots)
        if not sz.ok:
            event("sizing rejected: " + (sz.reason.split(" risks")[0] if sz.reason.startswith("minimum volume") else sz.reason))
            continue
        pos_objs = [o.as_position(spec.name, b.close) for o in opens]
        open_risk = sum(position_open_risk(spec, calc, p) for p in pos_objs)
        margin_used = sum(o.volume * spec.contract_size * o.entry / max(leverage, 1) for o in opens)
        new_margin = sz.volume * spec.contract_size * entry_ref / max(leverage, 1)
        ri = RiskInputs(
            equity=eq_now, balance=balance, free_margin=eq_now - margin_used, margin_used=margin_used,
            day_start_equity=day_start_equity, realized_today=realized_today, floating_bot=floating,
            consecutive_losses=consecutive, last_loss_ts=last_loss_ts,
            trades_last_hour=sum(1 for t in entry_times if t_close - t < 3600), risk_opened_today=risk_opened_today,
            open_positions=pos_objs, open_risk=open_risk)
        acct = AccountInfo(0, "backtest", "USD", balance, eq_now, margin_used, eq_now - margin_used, leverage, 0, True, True, 2)
        fails = check_entry(ri, risk_cfg, instrument, spec.name, side, sz.expected_loss, new_margin, acct, now=float(t_close))
        if fails:
            for f in fails:
                event(_bucket(f))
            continue
        pending = {"side": side, "signal_id": a.signal_id, "volume": sz.volume, "sl_dist": a.sl_distance,
                   "tp_dist": a.tp_distance, "risk": sz.expected_loss}

    # close anything still open at the last candle so every trade is counted (marked "END")
    if opens and last_i >= 0:
        lastb = m1[last_i]
        for pos in list(opens):
            px = lastb.close if pos.side == "BUY" else lastb.close + spread_of(lastb)
            close(pos, px, lastb.time + 60, "END")
    res.end_balance = balance
    return res


def _bucket(reason: str) -> str:
    """Group risk-gate messages that differ only by a number."""
    out, prev_digit = [], False
    for ch in reason:
        if ch.isdigit() or ch in ".%":
            if not prev_digit:
                out.append("#")
            prev_digit = True
        else:
            out.append(ch)
            prev_digit = False
    return "".join(out).replace("(# left)", "").replace("(#s left)", "").strip()


# ----------------------------------------------------------------------------------------------- splits and walk-forward
def split_in_out(t_start: int, t_end: int, oos_fraction: float) -> tuple[tuple[int, int], tuple[int, int]]:
    """Chronological split: the first (1 - oos_fraction) of the period is in-sample, the rest out-of-sample."""
    if not 0.0 < oos_fraction < 1.0:
        raise ValueError("oos_fraction must be between 0 and 1")
    cut = t_start + int((t_end - t_start) * (1.0 - oos_fraction))
    cut -= cut % 60
    return (t_start, cut), (cut, t_end)


def walk_forward_windows(t_start: int, t_end: int, train_days: float, test_days: float) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Rolling windows: train [s, s+train), test [s+train, s+train+test); s advances by test_days. Test windows never overlap."""
    if train_days <= 0 or test_days <= 0:
        raise ValueError("train_days and test_days must be positive")
    tr, te = int(train_days * 86400), int(test_days * 86400)
    out = []
    s = t_start
    while s + tr + te <= t_end:
        out.append(((s, s + tr), (s + tr, s + tr + te)))
        s += te
    return out


def parse_grid(text: str, cfg: dict[str, Any]) -> dict[str, list[float]]:
    """'tp_rr=1.2,1.6,2.0;sl_atr_mult=1.0,1.3' -> {'tp_rr': [...], 'sl_atr_mult': [...]} (strategy keys only)."""
    grid: dict[str, list[float]] = {}
    for part in filter(None, (p.strip() for p in text.split(";"))):
        if "=" not in part:
            raise ValueError(f"bad grid entry {part!r}")
        k, v = part.split("=", 1)
        k = k.strip()
        if k not in DEFAULT_CONFIG["strategy"]:
            raise ValueError(f"{k} is not a strategy setting")
        vals = [float(x) for x in v.split(",") if x.strip()]
        if not vals:
            raise ValueError(f"no values for {k}")
        grid[k] = vals
    return grid


def _with_params(cfg: dict[str, Any], instrument: str, params: dict[str, float]) -> dict[str, Any]:
    c = copy.deepcopy(cfg)
    c["instruments"][instrument].setdefault("strategy", {}).update(params)
    return c


def _score(res: BtResult, objective: str, min_trades: int) -> float:
    m = res.metrics()
    if m["trades"] < min_trades:
        return -math.inf
    if objective == "profit_factor":
        pf = m["profit_factor"]
        return 1e9 if pf == math.inf else (pf or 0.0)
    if objective == "net_pnl":
        return m["net_pnl"]
    return m["expectancy_r"]  # default: expectancy in R, comparable across risk sizes


def walk_forward(
    instrument: str, m1: Sequence[Bar], m5: Sequence[Bar], spec: SymbolSpec, cfg: dict[str, Any], windows: Sequence[tuple[tuple[int, int], tuple[int, int]]],
    m15: Optional[Sequence[Bar]] = None, grid: Optional[dict[str, list[float]]] = None, objective: str = "expectancy_r",
    min_trades: int = 10, **sim_kw: Any,
) -> dict[str, Any]:
    """Walk-forward test for ONE instrument. With a grid, each window picks parameters on its train period only and is
    then judged on its untouched test period; without a grid the fixed config is run over every test window."""
    out_windows = []
    oos_trades: list[Trade] = []
    for (tr, te) in windows:
        chosen: dict[str, float] = {}
        train_m = None
        if grid:
            keys = list(grid)
            best, best_score = None, -math.inf
            for combo in itertools.product(*(grid[k] for k in keys)):
                params = dict(zip(keys, combo))
                r = simulate(instrument, m1, m5, spec, _with_params(cfg, instrument, params), m15,
                             start_time=tr[0], end_time=tr[1], **sim_kw)
                s = _score(r, objective, min_trades)
                if s > best_score:
                    best, best_score, train_m = params, s, r.metrics()
            chosen = best or {}
        use_cfg = _with_params(cfg, instrument, chosen) if chosen else cfg
        if not grid:
            train_m = simulate(instrument, m1, m5, spec, use_cfg, m15, start_time=tr[0], end_time=tr[1], **sim_kw).metrics()
        test = simulate(instrument, m1, m5, spec, use_cfg, m15, start_time=te[0], end_time=te[1], **sim_kw)
        oos_trades.extend(test.trades)
        out_windows.append({"train": list(tr), "test": list(te), "chosen_params": chosen,
                            "train_metrics": train_m, "test_metrics": test.metrics()})
    sb = float(sim_kw.get("start_balance", 10000.0))
    t0 = windows[0][1][0] if windows else 0
    t1 = windows[-1][1][1] if windows else 0
    agg = metrics_from_trades(oos_trades, sb, t0, t1)
    profitable = sum(1 for w in out_windows if w["test_metrics"]["net_pnl"] > 0)
    return {"instrument": instrument, "tuned_for": instrument, "windows": out_windows, "objective": objective if grid else None,
            "out_of_sample": agg, "profitable_windows": profitable, "window_count": len(out_windows)}


# ----------------------------------------------------------------------------------------------- reporting and CLI
def _fmt_time(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


def format_metrics(title: str, m: dict[str, Any]) -> str:
    pf = m["profit_factor"]
    lines = [f"== {title} ==",
             f"trades {m['trades']} (wins {m['wins']}, losses {m['losses']}, win rate {m['win_rate_pct']}%)  per day {m['trades_per_day']}",
             f"net P&L {m['net_pnl']}  expectancy {m['expectancy']} per trade ({m['expectancy_r']} R)  profit factor {'inf' if pf == math.inf else pf}",
             f"avg win {m['avg_win']}  avg loss {m['avg_loss']}  payoff {m['payoff_ratio']}  longest losing streak {m['longest_losing_streak']}"]
    if "max_drawdown" in m:
        lines.append(f"max drawdown {m['max_drawdown']} ({m['max_drawdown_pct']}%)  return {m['return_pct']}%")
    else:
        lines.append(f"max drawdown (trade sequence) {m['trade_sequence_drawdown']} ({m['trade_sequence_drawdown_pct']}%)")
    c = m["costs"]
    lines.append(f"costs: spread {c['spread']} slippage {c['slippage']} commission {c['commission']} total {c['total']}")
    if m.get("risk_events"):
        lines.append("risk-limit events: " + ", ".join(f"{k} x{v}" for k, v in list(m["risk_events"].items())[:8]))
    return "\n".join(lines)


def _clean(o: Any) -> Any:
    if isinstance(o, float) and math.isinf(o):
        return "inf"
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def load_config(path: Optional[str], instrument: str) -> dict[str, Any]:
    """Defaults, optionally overlaid by a JSON file. A file tuned for another instrument is refused: parameters found for
    one instrument must be re-validated on their own data before they are used for another."""
    cfg = default_config()
    if not path:
        return cfg
    with open(path) as fh:
        over = json.load(fh)
    tuned = over.pop("tuned_for", None)
    if tuned and tuned != instrument:
        raise ValueError(f"{path} was tuned for {tuned}, not {instrument}; validate and tune each instrument independently")
    return deep_merge(cfg, over)


def _ts_arg(s: Optional[str]) -> Optional[int]:
    return _parse_time(s) if s else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scalper_engine.backtest", description="Backtest the M1 scalping strategy on CSV candles.")
    p.add_argument("--instrument", required=True, choices=sorted(DEFAULT_CONFIG["instruments"]))
    p.add_argument("--m1", required=True, help="M1 candles CSV (time,open,high,low,close,tick_volume,spread)")
    p.add_argument("--m5", help="M5 candles CSV; built from M1 when omitted")
    p.add_argument("--m15", help="optional M15 candles CSV (used only when strategy.use_m15 is true)")
    p.add_argument("--config", help="JSON overlay on the default config (may carry tuned_for)")
    p.add_argument("--from", dest="t_from", help="start (epoch or ISO, UTC)")
    p.add_argument("--to", dest="t_to", help="end (epoch or ISO, UTC)")
    p.add_argument("--balance", type=float, default=10000.0)
    p.add_argument("--leverage", type=int, default=100)
    p.add_argument("--default-spread-points", type=float, default=25.0, help="used for candles whose spread column is 0")
    p.add_argument("--commission-per-lot", type=float, help="round-turn commission per lot (account currency)")
    p.add_argument("--slippage-points", type=float, help="adverse slippage per fill, in points")
    p.add_argument("--risk-pct", type=float, help="risk per trade, percent of equity")
    for k in ("point", "tick-size", "tick-value", "contract-size", "vol-min", "vol-max", "vol-step"):
        p.add_argument(f"--{k}", type=float, help="contract spec override (use your broker's value)")
    p.add_argument("--digits", type=int)
    p.add_argument("--stops-level", type=int)
    p.add_argument("--freeze-level", type=int)
    p.add_argument("--oos-fraction", type=float, help="in-sample / out-of-sample split, e.g. 0.3 keeps the last 30%% untouched")
    p.add_argument("--walk-forward", metavar="TRAIN_DAYS,TEST_DAYS", help="rolling walk-forward, e.g. 30,7")
    p.add_argument("--grid", help="walk-forward parameter grid, e.g. 'tp_rr=1.2,1.6,2.0;sl_atr_mult=1.0,1.3'")
    p.add_argument("--objective", default="expectancy_r", choices=("expectancy_r", "profit_factor", "net_pnl"))
    p.add_argument("--min-trades", type=int, default=10, help="train windows with fewer trades are not eligible")
    p.add_argument("--out", help="write the full result as JSON")
    p.add_argument("--trades-csv", help="write every simulated trade (single run / out-of-sample) to CSV")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    cfg = load_config(a.config, a.instrument)
    ic = cfg["instruments"][a.instrument]
    if a.commission_per_lot is not None:
        ic["commission_per_lot"] = a.commission_per_lot
    if a.slippage_points is not None:
        ic["slippage_points"] = a.slippage_points
    if a.risk_pct is not None:
        cfg["risk"]["risk_per_trade_pct"] = a.risk_pct
    spec = make_spec(a.instrument, point=a.point, tick_size=a.tick_size, tick_value=a.tick_value, contract_size=a.contract_size,
                     volume_min=a.vol_min, volume_max=a.vol_max, volume_step=a.vol_step, digits=a.digits,
                     stops_level=a.stops_level, freeze_level=a.freeze_level)
    m1, rej1 = load_csv(a.m1)
    if a.m5:
        m5, rej5 = load_csv(a.m5)
    else:
        m5, rej5 = aggregate_bars(m1, 300), 0
    m15 = load_csv(a.m15)[0] if a.m15 else None
    if rej1 or rej5:
        print(f"warning: dropped {rej1} M1 and {rej5} M5 malformed/duplicate rows", file=sys.stderr)
    if len(m1) < WARMUP_BARS + 10:
        print("error: not enough M1 candles", file=sys.stderr)
        return 2
    t0 = _ts_arg(a.t_from) or m1[0].time + WARMUP_BARS * 60
    t1 = _ts_arg(a.t_to) or m1[-1].time + 60
    kw = dict(start_balance=a.balance, leverage=a.leverage, default_spread_points=a.default_spread_points)
    report: dict[str, Any] = {"instrument": a.instrument, "tuned_for": a.instrument, "period": [t0, t1],
                              "assumptions": "candle-based replay; see docs/BACKTESTING.md. Not a prediction of live results."}
    trades_out: list[Trade] = []
    if a.walk_forward:
        tr_d, te_d = (float(x) for x in a.walk_forward.split(","))
        wins = walk_forward_windows(t0, t1, tr_d, te_d)
        if not wins:
            print("error: the period is shorter than one train+test window", file=sys.stderr)
            return 2
        grid = parse_grid(a.grid, cfg) if a.grid else None
        wf = walk_forward(a.instrument, m1, m5, spec, cfg, wins, m15, grid, a.objective, a.min_trades, **kw)
        report["walk_forward"] = wf
        for k, w in enumerate(wf["windows"], 1):
            print(format_metrics(f"window {k} test {_fmt_time(w['test'][0])} -> {_fmt_time(w['test'][1])} params {w['chosen_params'] or 'fixed'}", w["test_metrics"]))
        print(format_metrics(f"OUT-OF-SAMPLE aggregate ({wf['profitable_windows']}/{wf['window_count']} windows profitable)", wf["out_of_sample"]))
    elif a.oos_fraction:
        (is_lo, is_hi), (oo_lo, oo_hi) = split_in_out(t0, t1, a.oos_fraction)
        ins = simulate(a.instrument, m1, m5, spec, cfg, m15, start_time=is_lo, end_time=is_hi, **kw)
        oos = simulate(a.instrument, m1, m5, spec, cfg, m15, start_time=oo_lo, end_time=oo_hi, **kw)
        report["in_sample"], report["out_of_sample"] = ins.metrics(), oos.metrics()
        print(format_metrics(f"IN-SAMPLE {_fmt_time(is_lo)} -> {_fmt_time(is_hi)}", report["in_sample"]))
        print(format_metrics(f"OUT-OF-SAMPLE {_fmt_time(oo_lo)} -> {_fmt_time(oo_hi)}", report["out_of_sample"]))
        trades_out = oos.trades
    else:
        r = simulate(a.instrument, m1, m5, spec, cfg, m15, start_time=t0, end_time=t1, **kw)
        report["result"] = r.metrics()
        print(format_metrics(f"{a.instrument} {_fmt_time(t0)} -> {_fmt_time(t1)}", report["result"]))
        trades_out = r.trades
    print("Backtest results are estimates from candle data and do not guarantee live performance.")
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(_clean(report), fh, indent=2)
    if a.trades_csv and trades_out:
        with open(a.trades_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["side", "open_time", "close_time", "volume", "entry", "exit", "original_sl", "final_sl", "risk", "pnl", "reason"])
            for t in trades_out:
                w.writerow([t.side, t.open_time, t.close_time, t.volume, t.entry, t.exit, t.original_sl, t.final_sl, round(t.risk, 2), round(t.pnl, 2), t.reason])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Position sizing and portfolio risk gates. Uses the broker's own contract data and account-currency maths."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .models import AccountInfo, Position, Sizing, SymbolSpec, floor_to_step, side_type


@dataclass
class RiskInputs:
    """Snapshot the gates evaluate. Built by the engine from broker + journal data."""
    equity: float
    balance: float
    free_margin: float
    margin_used: float
    day_start_equity: float
    realized_today: float
    floating_bot: float
    consecutive_losses: int
    last_loss_ts: float  # local epoch of the last losing close, 0 if none
    trades_last_hour: int
    risk_opened_today: float  # account-currency risk opened since UTC midnight
    open_positions: Sequence[Position] = ()
    open_risk: float = 0.0  # current aggregate risk of open bot positions (account currency)
    locked: bool = False
    lock_reason: str = ""


LossCalc = Callable[[str, str, float, float, float], Optional[float]]  # (side, symbol, volume, open, close) -> profit


def loss_at_stop(spec: SymbolSpec, calc: LossCalc, side: str, volume: float, entry: float, sl: float) -> float:
    """Account-currency loss if the stop is hit, as a positive number.

    Prefers the broker's order_calc_profit; falls back to tick value maths when the terminal cannot answer.
    """
    p = None
    try:
        p = calc(side, spec.name, volume, entry, sl)
    except Exception:
        p = None
    if p is not None:
        return abs(min(p, 0.0)) if p < 0 else 0.0
    tv = spec.tick_value_loss or spec.tick_value
    ticks = abs(entry - sl) / spec.tick_size if spec.tick_size else 0.0
    return ticks * tv * volume


def size_position(
    spec: SymbolSpec, calc: LossCalc, side: str, entry: float, sl: float, equity: float,
    risk_pct: float, commission_per_lot: float, slippage_points: float, max_lots: float = 0.0,
) -> Sizing:
    risk_amount = equity * risk_pct / 100.0
    if risk_amount <= 0 or equity <= 0:
        return Sizing(False, reason="no equity to risk")
    if abs(entry - sl) < spec.tick_size:
        return Sizing(False, reason="stop distance below one tick")
    base = loss_at_stop(spec, calc, side, 1.0, entry, sl)
    slip = slippage_points * spec.point * spec.value_per_price_per_lot
    per_lot = base + commission_per_lot + slip
    if per_lot <= 0:
        return Sizing(False, reason="could not compute loss per lot")
    raw = risk_amount / per_lot
    cap = spec.volume_max if spec.volume_max > 0 else raw
    if max_lots > 0:
        cap = min(cap, max_lots)
    vol = floor_to_step(min(raw, cap), spec.volume_step)  # always round DOWN
    if vol < spec.volume_min - 1e-12:
        need = spec.volume_min * per_lot
        return Sizing(False, 0.0, risk_amount, need, per_lot,
                      f"minimum volume {spec.volume_min} risks {need:.2f} > allowed {risk_amount:.2f}")
    loss = vol * per_lot
    if loss > risk_amount + 1e-9:  # defensive: cannot happen with floor, but never trust arithmetic blindly
        return Sizing(False, 0.0, risk_amount, loss, per_lot, "sized volume exceeds risk limit")
    return Sizing(True, round(vol, 8), round(risk_amount, 2), round(loss, 2), round(per_lot, 4))


def position_open_risk(spec: SymbolSpec, calc: LossCalc, p: Position) -> float:
    """Risk still at stake on an open position: loss if its CURRENT stop is hit (0 once the stop locks profit)."""
    if p.sl <= 0:
        # unprotected: treat as the worst case, a move of 5% of price
        return abs(p.price_open) * 0.05 * spec.contract_size * p.volume
    stop_loss_side = (p.sl < p.price_open) if p.side == "BUY" else (p.sl > p.price_open)
    if not stop_loss_side:
        return 0.0
    return loss_at_stop(spec, calc, p.side, p.volume, p.price_open, p.sl)


def check_entry(
    r: RiskInputs, risk_cfg: dict[str, Any], instrument: str, symbol: str, side: str, new_risk: float,
    new_margin: float, account: AccountInfo, now: Optional[float] = None,
) -> list[str]:
    """Return the list of risk-gate failures (empty means the entry is allowed)."""
    now = now if now is not None else time.time()
    out: list[str] = []
    if r.locked:
        out.append(f"trading locked: {r.lock_reason or 'risk lock'}")
    eq = max(r.equity, 1e-9)

    day_pl = r.realized_today + r.floating_bot
    if r.day_start_equity > 0 and -day_pl >= r.day_start_equity * risk_cfg["max_daily_loss_pct"] / 100.0:
        out.append("daily loss limit reached")
    if r.consecutive_losses >= risk_cfg["max_consecutive_losses"]:
        out.append(f"{r.consecutive_losses} consecutive losses")
    cd = float(risk_cfg.get("cooldown_after_loss_sec", 0))
    if cd and r.last_loss_ts and now - r.last_loss_ts < cd:
        out.append(f"cooldown after loss ({int(cd - (now - r.last_loss_ts))}s left)")
    if r.trades_last_hour >= risk_cfg.get("max_trades_per_hour", 999):
        out.append("max trades per hour reached")

    opens = list(r.open_positions)
    if len(opens) >= risk_cfg["max_total_positions"]:
        out.append("max total positions reached")
    same = [p for p in opens if p.symbol == symbol]
    if len(same) >= risk_cfg["max_positions_per_symbol"]:
        out.append("max positions for this symbol reached")
    if account.margin_mode != 2 and same:  # netting/exchange account: a second order would merge or reverse
        out.append("netting account: one position per symbol only")
    if any(p.side != side for p in same):
        out.append("opposite position open on this symbol (no reversals or hedges)")

    if (r.open_risk + new_risk) > eq * risk_cfg["max_aggregate_open_risk_pct"] / 100.0 + 1e-9:
        out.append("aggregate open risk limit")
    if (r.risk_opened_today + new_risk) > r.day_start_equity * risk_cfg["max_session_exposure_pct"] / 100.0 + 1e-9:
        out.append("session exposure limit")
    if new_margin > r.free_margin:
        out.append("insufficient free margin")
    used = (r.margin_used + new_margin) / eq * 100.0
    if used > risk_cfg["max_margin_utilization_pct"]:
        out.append(f"margin utilization {used:.1f}% over limit")
    return out


def daily_loss_breached(day_start_equity: float, realized: float, floating: float, max_pct: float) -> bool:
    return day_start_equity > 0 and -(realized + floating) >= day_start_equity * max_pct / 100.0

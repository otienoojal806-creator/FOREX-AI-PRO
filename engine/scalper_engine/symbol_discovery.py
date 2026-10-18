"""Find and verify the broker's real gold and crypto symbols. Never assume 'XAUUSD' or 'BTCUSD' exactly."""
from __future__ import annotations

import re
from typing import Any, Optional

from .models import SYMBOL_TRADE_MODE_FULL, SymbolBrief, SymbolSpec
from .mt5_connection import Broker

_KEYWORDS = {
    "XAUUSD": (("XAUUSD", "GOLD", "XAU"), ("EUR", "GBP", "AUD", "JPY", "CHF", "XAG", "SILVER")),
    "BTCUSD": (("BTCUSD", "BITCOIN", "BTC"), ("EUR", "GBP", "JPY", "ETH", "BCH", "LTC", "BTG")),
}


def _score(instrument: str, s: SymbolBrief) -> int:
    include, exclude = _KEYWORDS[instrument]
    name, text = s.name.upper(), f"{s.name} {s.path} {s.description}".upper()
    if not any(k in text for k in include):
        return 0
    if instrument == "XAUUSD" and ("XAG" in text or "SILVER" in text):
        return 0
    if instrument == "BTCUSD" and "BTC" in name and any(x in name.replace("BTCUSD", "") for x in exclude if x != "BTC"):
        # e.g. BTCEUR, BTCJPY: not USD-quoted
        if "USD" not in name:
            return 0
    score = 1
    if name == instrument:
        score += 100
    elif name.startswith(instrument):
        score += 60  # XAUUSD.m, XAUUSDm, XAUUSD#
    elif name in ("GOLD", "BITCOIN") or name.startswith(("GOLD", "BITCOIN")):
        score += 40
    if "USD" in name or "USD" in text:
        score += 10
    if s.trade_mode == SYMBOL_TRADE_MODE_FULL:
        score += 20
    if s.visible:
        score += 5
    return score


def discover(broker: Broker, instruments: tuple[str, ...] = ("XAUUSD", "BTCUSD"), limit: int = 6) -> dict[str, list[dict[str, Any]]]:
    syms = broker.symbols() or []
    out: dict[str, list[dict[str, Any]]] = {}
    for inst in instruments:
        ranked = sorted(((_score(inst, s), s) for s in syms), key=lambda x: -x[0])
        out[inst] = [
            {"name": s.name, "path": s.path, "description": s.description[:60], "visible": s.visible,
             "trade_mode": s.trade_mode, "tradable": s.trade_mode == SYMBOL_TRADE_MODE_FULL, "score": sc}
            for sc, s in ranked if sc > 0
        ][:limit]
    return out


def verify_mapping(broker: Broker, instrument: str, broker_symbol: str) -> tuple[bool, list[str], Optional[SymbolSpec]]:
    """Check that a mapped symbol exists, is selectable, fully tradable and has sane contract data."""
    issues: list[str] = []
    if not broker_symbol:
        return False, ["no broker symbol configured"], None
    if not broker.symbol_select(broker_symbol):
        issues.append(f"{broker_symbol} cannot be added to Market Watch")
    spec = broker.symbol_spec(broker_symbol)
    if spec is None:
        return False, issues + [f"{broker_symbol} not found at this broker"], None
    if spec.trade_mode != SYMBOL_TRADE_MODE_FULL:
        issues.append(f"{broker_symbol} is not open for full trading (mode {spec.trade_mode})")
    if not (spec.point > 0 and spec.tick_size > 0 and spec.tick_value > 0 and spec.contract_size > 0):
        issues.append("contract specification incomplete (point/tick/contract size)")
    if not (spec.volume_min > 0 and spec.volume_step > 0 and spec.volume_max >= spec.volume_min):
        issues.append("volume limits invalid")
    kw = _KEYWORDS[instrument][0]
    if not any(k in f"{spec.name} {spec.path} {spec.description}".upper() for k in kw):
        issues.append(f"{broker_symbol} does not look like {instrument}; check the mapping")
    return (not issues), issues, spec

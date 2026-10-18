"""Export candles (and the broker's contract spec) from a running MT5 terminal to CSV for backtesting.

Windows only (the MetaTrader5 package talks to the installed terminal). Read-only: it never sends orders.

Example:
  python scripts/export_rates.py --symbol XAUUSD --timeframe M1 --days 60 --out data/xauusd_m1.csv
  python scripts/export_rates.py --symbol XAUUSD --timeframe M5 --days 60 --out data/xauusd_m5.csv

Use the BROKER's symbol name (it may be XAUUSD.m, GOLD, BTCUSD#...). The spec JSON written next to the CSV holds the
broker's tick size, contract size and volume rules; pass those values to the backtest (--tick-value, --contract-size, ...).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone

TF = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", required=True, help="broker symbol, e.g. XAUUSD")
    p.add_argument("--timeframe", default="M1", choices=sorted(TF))
    p.add_argument("--days", type=float, default=30, help="how many days back from now")
    p.add_argument("--from", dest="t_from", help="UTC start, e.g. 2026-08-01 (overrides --days)")
    p.add_argument("--to", dest="t_to", help="UTC end (default: now)")
    p.add_argument("--path", default=os.environ.get("MT5_PATH", ""), help="terminal64.exe path (default: MT5_PATH env var)")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError:
        print("The MetaTrader5 package is not installed (pip install MetaTrader5). It only runs on Windows.", file=sys.stderr)
        return 2
    ok = mt5.initialize(a.path) if a.path else mt5.initialize()
    if not ok:
        print(f"MT5 initialize failed: {mt5.last_error()}", file=sys.stderr)
        return 2
    try:
        if not mt5.symbol_select(a.symbol, True):
            print(f"symbol {a.symbol} not available: {mt5.last_error()}", file=sys.stderr)
            return 2
        end = datetime.fromisoformat(a.t_to).replace(tzinfo=timezone.utc) if a.t_to else datetime.now(timezone.utc)
        start = datetime.fromisoformat(a.t_from).replace(tzinfo=timezone.utc) if a.t_from else end - timedelta(days=a.days)
        rates = mt5.copy_rates_range(a.symbol, getattr(mt5, TF[a.timeframe]), start, end)
        if rates is None or len(rates) == 0:
            print(f"no candles returned: {mt5.last_error()}. Scroll the chart / raise 'Max bars in chart' in MT5 so history is downloaded.", file=sys.stderr)
            return 3
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "open", "high", "low", "close", "tick_volume", "spread"])
            for r in rates:  # the last candle may still be forming; the backtest drops nothing, so skip it here
                w.writerow([int(r["time"]), r["open"], r["high"], r["low"], r["close"], int(r["tick_volume"]), int(r["spread"])])
            fh.flush()
        # drop the still-forming final candle
        with open(a.out) as fh:
            lines = fh.read().splitlines()
        with open(a.out, "w", newline="") as fh:
            fh.write("\n".join(lines[:-1]) + "\n")
        info = mt5.symbol_info(a.symbol)
        if info is not None:
            spec = {k: getattr(info, k) for k in ("digits", "point", "trade_tick_size", "trade_tick_value", "trade_tick_value_profit",
                                                    "trade_tick_value_loss", "trade_contract_size", "volume_min", "volume_max",
                                                    "volume_step", "trade_stops_level", "trade_freeze_level", "currency_profit")}
            with open(os.path.splitext(a.out)[0] + ".spec.json", "w") as fh:
                json.dump({"symbol": a.symbol, **spec}, fh, indent=2)
        print(f"wrote {len(rates) - 1} completed {a.timeframe} candles to {a.out}")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

# Backtesting and walk-forward testing

`scalper_engine.backtest` replays historical candles through the **same code the live engine uses** for signals
(`strategy_engine.evaluate`), position sizing (`size_position`), every risk gate (`check_entry`) and the trailing-stop
decision (`compute_new_stop`). It needs only the Python standard library, so it runs on any machine, not just the MT5 VPS.

> **Results are estimates, not promises.** A backtest cannot know your real fills, requotes, spread spikes, news gaps,
> swap, or how the strategy behaves in a market that does not look like the sample. A good backtest does not guarantee
> live profit; a bad one is a strong warning. Always finish with the broker **demo** account before going live.

## 1. Get data

On the Windows machine that runs MT5 (read-only, sends no orders):

```
python scripts\export_rates.py --symbol XAUUSD --timeframe M1 --days 90 --out data\xauusd_m1.csv
python scripts\export_rates.py --symbol XAUUSD --timeframe M5 --days 90 --out data\xauusd_m5.csv
```

Use your broker's exact symbol name. Each export also writes `*.spec.json` with the broker's tick size, tick value,
contract size and volume rules. Pass those to the backtest (flags below) so sizing and cost maths match your account.
If MT5 returns too few candles, raise **Max bars in chart** (Tools > Options > Charts) and scroll the chart back to
download history. Copy the CSVs to wherever you run the backtest (it also works on Linux/macOS).

CSV format: `time,open,high,low,close,tick_volume,spread` (time = epoch seconds or ISO UTC; spread in **points**).
Rows that are malformed, have `high < low`, non-positive prices or duplicate timestamps are dropped and counted
(printed as a warning), never silently kept. `--m5` is optional: it is built from M1 when omitted.

## 2. Run

```
# one run over everything
python -m scalper_engine.backtest --instrument XAUUSD --m1 data\xauusd_m1.csv --m5 data\xauusd_m5.csv ^
    --tick-value 1 --contract-size 100 --commission-per-lot 7 --slippage-points 8 --out result.json

# in-sample / out-of-sample: the last 30% is only looked at once, after you are done tuning
python -m scalper_engine.backtest --instrument XAUUSD --m1 ... --oos-fraction 0.3

# walk-forward: 30 days train, 7 days test, rolling; fixed settings
python -m scalper_engine.backtest --instrument XAUUSD --m1 ... --walk-forward 30,7

# walk-forward with parameter selection on the TRAIN window only, judged on the untouched TEST window
python -m scalper_engine.backtest --instrument XAUUSD --m1 ... --walk-forward 30,7 ^
    --grid "tp_rr=1.2,1.6,2.0;sl_atr_mult=1.0,1.3" --objective expectancy_r --min-trades 20
```

Other options: `--config file.json` (overlay on the default config), `--from/--to`, `--balance`, `--leverage`,
`--risk-pct`, `--default-spread-points` (used when a candle's spread column is 0), `--trades-csv trades.csv`,
and spec overrides `--point --digits --tick-size --tick-value --contract-size --vol-min --vol-max --vol-step
--stops-level --freeze-level`. Without spec flags the built-in XAUUSD/BTCUSD presets are used; treat those as rough.

## 3. What is reported

Trades, win rate, **expectancy** (average net result per trade, in account currency and in R), **profit factor**,
average win / average loss and payoff ratio, **max drawdown** (from a per-candle equity curve, including open trades),
trades per day, longest losing streak, **total costs** split into spread, slippage and commission (these are already
inside the P&L, shown so you can see how much they eat), exit reasons (SL / TP / TRAILING_SL / END) and
**risk-limit events** (how many entries each gate blocked: daily loss, consecutive losses, cooldown, max positions,
session exposure, margin, sizing too risky...).

## 4. Execution assumptions (all pessimistic where candles cannot tell the truth)

- A signal comes from a completed M1 candle and is filled at the **next candle's open** (BUY at ask = bid + that
  candle's spread, SELL at bid), then adverse slippage (`slippage_points`) is applied.
- BUY stops/targets trigger on bid, SELL on ask. If one candle touches both stop and target, the **stop** is assumed first.
  A stop that gaps fills at the open (worse) and pays slippage. Targets fill at the target price.
- Commission (`commission_per_lot`, round turn) is charged at close.
- Trailing is evaluated once per candle close and only protects from the next candle. The live engine trails on every tick,
  so live trailing can lock profit that this replay cannot see (and can also be stopped out by wicks the replay merges).
  Stops are never loosened, exactly like the live manager.
- Risk gates see the simulated account: equity, open risk, margin (`leverage`), daily loss, cooldown, trades/hour.
  Consecutive-loss and daily counters reset each UTC day (live trading needs a human to resume, a replay cannot wait for one).
- Broker server time is treated as UTC for session filters; if your broker's server clock is offset, shift `sessions_utc`.
- Not modelled: swap/rollover, requotes, partial fills, stop-level rejections, broker-side news freezes, variable
  per-tick spread inside a candle, and any live latency beyond the one-candle fill delay.

## 5. Avoiding self-deception

- **Tune each instrument on its own data.** Gold and BTC differ in volatility, contract size and sessions. A `--config`
  file written for BTCUSD with `"tuned_for": "BTCUSD"` is **refused** for XAUUSD, and walk-forward output carries
  `tuned_for`. Do not copy parameters across instruments without re-validating them on that instrument's out-of-sample data.
- Choose parameters on in-sample / train data only. Look at out-of-sample once. If you keep re-tuning until the
  out-of-sample looks good, it is no longer out-of-sample.
- Prefer **walk-forward**: the aggregate out-of-sample line is the number to trust, together with how many windows were
  profitable. A strategy that only works in one window is probably noise.
- Keep grids small. Many combinations on little data will always find something that looks great by luck.
- Look for enough trades (hundreds, not dozens) and check that results survive doubled spread and slippage.
- Check the risk-event counts: a run that would have been halted by the daily-loss or consecutive-loss gate every week
  is telling you something.

## 6. Tests

`python -m unittest tests.test_backtest` covers the stop-versus-target ordering, gaps, bid/ask handling, metric maths,
stop monotonicity, volume stepping and risk budget, daily-loss and position gates, session filter, split and
walk-forward windows, CSV validation and the CLI.

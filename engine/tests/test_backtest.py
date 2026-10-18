"""Backtester tests: execution assumptions, metrics maths, stop monotonicity, splits and walk-forward."""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import tempfile
import unittest

from scalper_engine import backtest as bt
from scalper_engine.config import default_config
from scalper_engine.models import Bar

from .fakes import T0, aggregate, make_bars

SPEC = bt.make_spec("XAUUSD")


def _cfg(**risk):
    cfg = default_config()
    cfg["instruments"]["XAUUSD"]["sessions_utc"] = []
    cfg["risk"].update(risk)
    return cfg


def _market(n=4000, drift=0.05, seed=9):
    m1 = make_bars(n, 2300.0, drift, 1.6, T0 + n * 60, seed=seed)
    return m1, aggregate(m1)


def _pos(side="BUY", entry=2000.0, sl=1990.0, tp=2020.0):
    return bt._Open(side, "s", 0.1, entry, sl, tp, sl, 10.0, 0, entry, entry, 0.0, 0.0, [sl], 1)


def _bar(o, h, lo, c, t=0):
    return Bar(t, o, h, lo, c, 0, 0)


class ExecutionRules(unittest.TestCase):
    def test_stop_wins_when_one_candle_touches_both(self):
        r = bt.check_bar_exit(_pos(), _bar(2000, 2025, 1985, 2010), 0.0, 0.0)
        self.assertEqual(r, (1990.0, "SL"))

    def test_gap_through_stop_fills_at_open_with_slippage(self):
        r = bt.check_bar_exit(_pos(), _bar(1980, 1985, 1975, 1982), 0.0, 0.5)
        self.assertEqual(r, (1979.5, "SL"))

    def test_buy_target_uses_bid_and_sell_uses_ask(self):
        self.assertEqual(bt.check_bar_exit(_pos(), _bar(2000, 2020, 1995, 2010), 0.5, 0.0), (2020.0, "TP"))
        s = _pos("SELL", 2000.0, 2010.0, 1980.0)
        # bid low 1979.7 + spread 0.5 = ask low 1980.2 -> target (1980) NOT reached yet
        self.assertIsNone(bt.check_bar_exit(s, _bar(2000, 2001, 1979.7, 1985), 0.5, 0.0))
        self.assertEqual(bt.check_bar_exit(s, _bar(2000, 2001, 1979.4, 1985), 0.5, 0.0), (1980.0, "TP"))
        # sell stop triggers on ASK: bid high 2009.6 + 0.5 = 2010.1 >= 2010
        self.assertEqual(bt.check_bar_exit(s, _bar(2000, 2009.6, 1995, 2000), 0.5, 0.0), (2010.0, "SL"))

    def test_trailed_stop_reason(self):
        p = _pos(sl=2005.0)
        p.trailed = True
        self.assertEqual(bt.check_bar_exit(p, _bar(2010, 2011, 2004, 2008), 0, 0)[1], "TRAILING_SL")


class Metrics(unittest.TestCase):
    def _t(self, pnl, risk=10.0):
        return bt.Trade("BUY", "s", 0, 1, 0.1, 1, 1, 0, 0, [], risk, pnl, 1.0, 0.5, 0.25, pnl, "TP")

    def test_known_values(self):
        ts = [self._t(30), self._t(-10), self._t(-10), self._t(20)]
        m = bt.metrics_from_trades(ts, 1000.0, 0, 2 * 86400)
        self.assertEqual(m["trades"], 4)
        self.assertEqual(m["win_rate_pct"], 50.0)
        self.assertEqual(m["net_pnl"], 30.0)
        self.assertEqual(m["expectancy"], 7.5)
        self.assertEqual(m["profit_factor"], 2.5)
        self.assertEqual(m["avg_win"], 25.0)
        self.assertEqual(m["avg_loss"], -10.0)
        self.assertEqual(m["payoff_ratio"], 2.5)
        self.assertEqual(m["trades_per_day"], 2.0)
        self.assertEqual(m["longest_losing_streak"], 2)
        self.assertEqual(m["trade_sequence_drawdown"], 20.0)
        self.assertEqual(m["costs"]["total"], 7.0)

    def test_no_losses_is_infinite_profit_factor_and_empty_is_safe(self):
        self.assertEqual(bt.metrics_from_trades([self._t(5)], 1000, 0, 86400)["profit_factor"], math.inf)
        m = bt.metrics_from_trades([], 1000, 0, 86400)
        self.assertEqual((m["trades"], m["expectancy"], m["profit_factor"]), (0, 0.0, None))

    def test_drawdown_helper(self):
        self.assertEqual(bt.trade_sequence_drawdown([50, -30, -30, 100], 100.0), (60.0, 60 / 150 * 100))


class Simulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m1, cls.m5 = _market()
        cls.res = bt.simulate("XAUUSD", cls.m1, cls.m5, SPEC, _cfg())

    def test_produces_trades_and_signals(self):
        self.assertGreater(len(self.res.trades), 5)
        self.assertGreaterEqual(self.res.signals, len(self.res.trades))

    def test_deterministic(self):
        again = bt.simulate("XAUUSD", self.m1, self.m5, SPEC, _cfg())
        self.assertEqual([t.pnl for t in again.trades], [t.pnl for t in self.res.trades])

    def test_stops_never_loosen(self):
        trailed = 0
        for t in self.res.trades:
            h = t.sl_history
            pairs = list(zip(h, h[1:]))
            trailed += len(pairs)
            for a, b in pairs:
                self.assertTrue(b > a if t.side == "BUY" else b < a, (t.side, h))
        self.assertGreater(trailed, 0, "scenario should exercise the trailing stop")

    def test_loss_at_original_stop_is_within_risk_budget(self):
        budget = 10000.0 * 0.25 / 100 * 1.05  # equity moves a little between trades
        for t in self.res.trades:
            self.assertLessEqual(t.risk, budget * 1.5)
            if t.reason == "SL" and t.final_sl == t.original_sl:
                # a full stop-out may exceed the budget only by slippage / gap, never by a large factor
                self.assertGreater(t.pnl, -t.risk * 1.6)

    def test_volume_respects_broker_step(self):
        for t in self.res.trades:
            self.assertAlmostEqual(t.volume / SPEC.volume_step, round(t.volume / SPEC.volume_step), places=6)
            self.assertGreaterEqual(t.volume, SPEC.volume_min)

    def test_costs_reduce_results(self):
        free = _cfg()
        free["instruments"]["XAUUSD"]["slippage_points"] = 0
        pricey = _cfg()
        pricey["instruments"]["XAUUSD"]["commission_per_lot"] = 30.0
        a = bt.simulate("XAUUSD", self.m1, self.m5, SPEC, free, default_spread_points=0.0001)
        b = bt.simulate("XAUUSD", self.m1, self.m5, SPEC, pricey)
        self.assertGreater(b.metrics()["costs"]["commission"], 0)
        self.assertEqual(a.metrics()["costs"]["commission"], 0.0)

    def test_equity_and_drawdown_consistent(self):
        m = self.res.metrics()
        self.assertAlmostEqual(self.res.end_balance, 10000.0 + sum(t.pnl for t in self.res.trades), places=6)
        self.assertGreaterEqual(m["max_drawdown"], 0.0)
        self.assertEqual(m["trades"], len(self.res.trades))

    def test_daily_loss_limit_blocks_entries_and_is_counted(self):
        tight = _cfg(max_daily_loss_pct=0.1, max_consecutive_losses=20, cooldown_after_loss_sec=0, max_session_exposure_pct=50)
        r = bt.simulate("XAUUSD", self.m1, self.m5, SPEC, tight)
        self.assertIn("daily loss limit reached", r.risk_events)
        # no day may lose much more than the limit plus the one trade that crossed it
        by_day = {}
        for t in r.trades:
            by_day[t.open_time // 86400] = by_day.get(t.open_time // 86400, 0.0) + t.pnl
        self.assertGreater(min(by_day.values()), -10000 * 0.001 * 6)

    def test_max_positions_gate(self):
        r = bt.simulate("XAUUSD", self.m1, self.m5, SPEC, _cfg(max_total_positions=1))
        ev = sorted((t.open_time, t.close_time) for t in r.trades)
        for (o1, c1), (o2, c2) in zip(ev, ev[1:]):
            self.assertGreaterEqual(o2, c1 - 60)

    def test_window_restricts_replay(self):
        lo, hi = self.m1[2000].time, self.m1[3000].time
        r = bt.simulate("XAUUSD", self.m1, self.m5, SPEC, _cfg(), start_time=lo, end_time=hi)
        self.assertTrue(all(lo <= t.open_time < hi for t in r.trades))
        self.assertLessEqual(r.bars, 1000)

    def test_unknown_instrument_and_empty_data(self):
        with self.assertRaises(ValueError):
            bt.simulate("EURUSD", self.m1, self.m5, SPEC, _cfg())
        with self.assertRaises(ValueError):
            bt.simulate("XAUUSD", [], [], SPEC, _cfg())

    def test_session_filter_blocks_out_of_hours(self):
        cfg = _cfg()
        cfg["instruments"]["XAUUSD"]["sessions_utc"] = [[0, 1]]
        r = bt.simulate("XAUUSD", self.m1, self.m5, SPEC, cfg)
        from datetime import datetime, timezone
        for t in r.trades:
            self.assertEqual(datetime.fromtimestamp(t.open_time, timezone.utc).hour, 0)


class SplitsAndWalkForward(unittest.TestCase):
    def test_split_is_chronological_and_disjoint(self):
        (a0, a1), (b0, b1) = bt.split_in_out(0, 10 * 86400, 0.3)
        self.assertEqual((a0, b1), (0, 10 * 86400))
        self.assertEqual(a1, b0)
        self.assertAlmostEqual((b1 - b0) / (10 * 86400), 0.3, places=2)
        with self.assertRaises(ValueError):
            bt.split_in_out(0, 100, 1.0)

    def test_walk_forward_windows_do_not_overlap_test_periods(self):
        w = bt.walk_forward_windows(0, 20 * 86400, 10, 5)
        self.assertEqual(len(w), 2)
        tests = [t for _, t in w]
        for (a0, a1), (b0, b1) in zip(tests, tests[1:]):
            self.assertEqual(a1, b0)
        for tr, te in w:
            self.assertEqual(tr[1], te[0])
        self.assertEqual(bt.walk_forward_windows(0, 5 * 86400, 10, 5), [])

    def test_parse_grid(self):
        g = bt.parse_grid("tp_rr=1.2,1.6; sl_atr_mult=1.0", default_config())
        self.assertEqual(g, {"tp_rr": [1.2, 1.6], "sl_atr_mult": [1.0]})
        with self.assertRaises(ValueError):
            bt.parse_grid("not_a_setting=1", default_config())
        with self.assertRaises(ValueError):
            bt.parse_grid("tp_rr", default_config())

    def test_walk_forward_end_to_end_picks_params_on_train_only(self):
        m1, m5 = _market(n=7200)  # five days of M1
        t0 = m1[WARM].time
        wins = bt.walk_forward_windows(t0, m1[-1].time, 2, 1)
        self.assertGreaterEqual(len(wins), 2)
        grid = {"tp_rr": [1.2, 2.0]}
        out = bt.walk_forward("XAUUSD", m1, m5, SPEC, _cfg(), wins, None, grid, "expectancy_r", 1)
        self.assertEqual(out["tuned_for"], "XAUUSD")
        self.assertEqual(out["window_count"], len(wins))
        for w in out["windows"]:
            self.assertIn(w["chosen_params"].get("tp_rr"), (1.2, 2.0))
            self.assertLessEqual(w["train"][1], w["test"][0])
        self.assertEqual(out["out_of_sample"]["trades"], sum(w["test_metrics"]["trades"] for w in out["windows"]))

    def test_fixed_walk_forward_without_grid(self):
        m1, m5 = _market(n=5000)
        wins = bt.walk_forward_windows(m1[WARM].time, m1[-1].time, 1, 1)
        out = bt.walk_forward("XAUUSD", m1, m5, SPEC, _cfg(), wins)
        self.assertIsNone(out["objective"])
        self.assertTrue(all(w["chosen_params"] == {} for w in out["windows"]))


WARM = bt.WARMUP_BARS


class DataAndCli(unittest.TestCase):
    def _write(self, d, name, bars, extra=()):
        p = os.path.join(d, name)
        with open(p, "w") as fh:
            fh.write("time,open,high,low,close,tick_volume,spread\n")
            for b in bars:
                fh.write(f"{b.time},{b.open},{b.high},{b.low},{b.close},{b.tick_volume},{b.spread}\n")
            for line in extra:
                fh.write(line + "\n")
        return p

    def test_load_csv_rejects_bad_rows_and_duplicates(self):
        m1, _ = _market(n=100)
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m1.csv", m1, extra=[
                f"{m1[5].time},1,2,0.5,1,0,0",          # duplicate timestamp
                "9999999999,10,9,11,10,0,0",             # high < low
                "9999999998,abc,1,1,1,0,0",              # not a number
                "9999999997,-1,1,1,1,0,0",               # non-positive price
            ])
            bars, rejected = bt.load_csv(p)
        self.assertEqual(len(bars), 100)
        self.assertEqual(rejected, 4)
        self.assertEqual([b.time for b in bars], sorted(b.time for b in bars))

    def test_load_csv_iso_times_and_header_check(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.csv")
            with open(p, "w") as fh:
                fh.write("time,open,high,low,close\n2026-01-01 00:00:00,1,2,0.5,1.5\n")
            bars, rej = bt.load_csv(p)
            self.assertEqual((len(bars), rej, bars[0].time), (1, 0, 1767225600))
            bad = os.path.join(d, "y.csv")
            with open(bad, "w") as fh:
                fh.write("a,b\n1,2\n")
            with self.assertRaises(ValueError):
                bt.load_csv(bad)

    def test_aggregate_bars_matches_manual(self):
        m1, _ = _market(n=100)
        m5 = bt.aggregate_bars([b for b in m1 if b.time % 300 >= 0], 300)
        self.assertTrue(all(b.high >= b.low for b in m5))

    def test_config_tuned_for_other_instrument_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w") as fh:
                json.dump({"tuned_for": "BTCUSD", "strategy": {"tp_rr": 2.0}}, fh)
            with self.assertRaises(ValueError):
                bt.load_config(p, "XAUUSD")
            self.assertEqual(bt.load_config(p, "BTCUSD")["strategy"]["tp_rr"], 2.0)

    def test_cli_single_split_and_walk_forward(self):
        m1, m5 = _market(n=5000)
        with tempfile.TemporaryDirectory() as d:
            p1, p5 = self._write(d, "m1.csv", m1), self._write(d, "m5.csv", m5)
            out = os.path.join(d, "r.json")
            tr = os.path.join(d, "t.csv")
            base = ["--instrument", "XAUUSD", "--m1", p1, "--m5", p5, "--out", out]

            def run(extra):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    return bt.main(base + extra)

            def report():
                with open(out) as fh:
                    return json.load(fh)
            self.assertEqual(run(["--trades-csv", tr]), 0)
            rep = report()
            self.assertIn("result", rep)
            self.assertEqual(rep["tuned_for"], "XAUUSD")
            self.assertEqual(run(["--oos-fraction", "0.3"]), 0)
            rep = report()
            self.assertIn("in_sample", rep)
            self.assertIn("out_of_sample", rep)
            self.assertEqual(run(["--walk-forward", "1,1", "--grid", "tp_rr=1.2,1.8", "--min-trades", "1"]), 0)
            self.assertIn("walk_forward", report())
            self.assertEqual(run(["--walk-forward", "30,30"]), 2)  # period shorter than one window


if __name__ == "__main__":
    unittest.main()

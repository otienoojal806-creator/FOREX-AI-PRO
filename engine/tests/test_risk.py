import random
import unittest

from scalper_engine.config import default_config
from scalper_engine.models import Position
from scalper_engine.risk_manager import (
    RiskInputs, check_entry, daily_loss_breached, loss_at_stop, position_open_risk, size_position,
)

from .fakes import FakeBroker, account, btc_spec, gold_spec
from .scenarios import ok_risk_inputs

RC = default_config()["risk"]


class SizingTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBroker()

    def size(self, symbol, entry, sl, equity=10000.0, pct=0.25, slip=0.0, comm=0.0, max_lots=0.0, side="BUY"):
        return size_position(self.b.specs[symbol], self.b.calc_profit, side, entry, sl, equity, pct, comm, slip, max_lots)

    def test_gold_volume_uses_contract_size_and_floors_to_step(self):
        s = self.size("XAUUSD", 2400.25, 2397.25)  # $300 loss per lot, risk $25 -> 0.0833 -> 0.08
        self.assertTrue(s.ok)
        self.assertEqual(s.volume, 0.08)
        self.assertAlmostEqual(s.expected_loss, 24.0)
        self.assertLessEqual(s.expected_loss, 25.0)

    def test_rounds_down_even_when_nearest_step_is_closer(self):
        # raw volume 0.0899: the nearest step (0.09) would risk $25.02 > $25, so the answer must be 0.08, not a rejection
        s = self.size("XAUUSD", 2400.0, 2397.22)  # 2.78 move -> $278 per lot
        self.assertTrue(s.ok, s.reason)
        self.assertEqual(s.volume, 0.08)

    def test_crypto_and_gold_differ_because_contracts_differ(self):
        gold = self.size("XAUUSD", 2400.0, 2397.0)    # 3.00 move * 100 oz
        btc = self.size("BTCUSD", 60000.0, 59600.0)   # 400 move * 1 coin
        self.assertAlmostEqual(gold.loss_per_lot, 300.0)
        self.assertAlmostEqual(btc.loss_per_lot, 400.0)
        self.assertNotEqual(gold.volume, btc.volume)
        for s in (gold, btc):
            self.assertLessEqual(s.expected_loss, 25.0 + 1e-9)

    def test_costs_reduce_volume(self):
        free = self.size("XAUUSD", 2400.0, 2397.0)
        costly = self.size("XAUUSD", 2400.0, 2397.0, slip=500, comm=7.0)
        self.assertLess(costly.volume, free.volume)
        self.assertGreater(costly.loss_per_lot, free.loss_per_lot)

    def test_rejects_when_minimum_volume_is_too_risky(self):
        s = self.size("XAUUSD", 2400.0, 2397.0, equity=500.0)  # allowed $1.25, min lot risks $3
        self.assertFalse(s.ok)
        self.assertEqual(s.volume, 0.0)
        self.assertIn("minimum volume", s.reason)

    def test_rejects_stop_inside_one_tick_and_no_equity(self):
        self.assertFalse(self.size("XAUUSD", 2400.0, 2400.0).ok)
        self.assertFalse(self.size("XAUUSD", 2400.0, 2397.0, equity=0.0).ok)

    def test_max_lots_and_broker_max_cap_volume(self):
        s = self.size("XAUUSD", 2400.0, 2399.0, equity=1_000_000.0, max_lots=1.0)
        self.assertEqual(s.volume, 1.0)

    def test_never_exceeds_risk_budget_property(self):
        rnd = random.Random(5)
        for _ in range(3000):
            sym = rnd.choice(["XAUUSD", "BTCUSD"])
            entry = rnd.uniform(1800, 2800) if sym == "XAUUSD" else rnd.uniform(20000, 90000)
            dist = rnd.uniform(0.2, 12) if sym == "XAUUSD" else rnd.uniform(30, 2500)
            side = rnd.choice(["BUY", "SELL"])
            sl = entry - dist if side == "BUY" else entry + dist
            eq = rnd.uniform(200, 200000)
            pct = rnd.choice([0.1, 0.25, 0.5, 1.0])
            s = self.size(sym, entry, sl, equity=eq, pct=pct, slip=rnd.choice([0, 8, 600]), comm=rnd.choice([0, 3.5]), side=side)
            if s.ok:
                budget = eq * pct / 100
                # exact loss (volume x loss-per-lot) must fit the budget; the reported figures are rounded to cents
                self.assertLessEqual(s.volume * s.loss_per_lot, budget + 0.01, (sym, entry, dist, eq, pct, s))
                self.assertLessEqual(s.expected_loss, budget + 0.005, (sym, entry, dist, eq, pct, s))
                self.assertGreaterEqual(s.volume, self.b.specs[sym].volume_min - 1e-12)
                self.assertAlmostEqual(s.volume / self.b.specs[sym].volume_step, round(s.volume / self.b.specs[sym].volume_step), places=6)

    def test_falls_back_to_tick_value_when_terminal_cannot_calculate(self):
        spec = gold_spec()
        loss = loss_at_stop(spec, lambda *a: None, "BUY", 1.0, 2400.0, 2397.0)
        self.assertAlmostEqual(loss, 300.0)  # 300 ticks * $1
        loss = loss_at_stop(spec, lambda *a: (_ for _ in ()).throw(RuntimeError("x")), "BUY", 1.0, 2400.0, 2397.0)
        self.assertAlmostEqual(loss, 300.0)

    def test_open_risk_is_zero_once_stop_locks_profit_and_worst_case_without_stop(self):
        spec = gold_spec()
        calc = self.b.calc_profit
        buy_risk = Position(1, "XAUUSD", "BUY", 0.1, 2400.0, 2397.0, 2406.0)
        locked = Position(2, "XAUUSD", "BUY", 0.1, 2400.0, 2401.0, 2406.0)
        naked = Position(3, "XAUUSD", "BUY", 0.1, 2400.0, 0.0, 2406.0)
        self.assertAlmostEqual(position_open_risk(spec, calc, buy_risk), 30.0)
        self.assertEqual(position_open_risk(spec, calc, locked), 0.0)
        self.assertGreater(position_open_risk(spec, calc, naked), 1000.0)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.acct = account()
        self.rc = dict(RC)

    def gates(self, **kw):
        ri = ok_risk_inputs(self.acct, **kw)
        return check_entry(ri, self.rc, "XAUUSD", "XAUUSD", "BUY", 24.0, 500.0, self.acct, now=10_000.0)

    def test_clean_state_allows_entry(self):
        self.assertEqual(self.gates(), [])

    def test_daily_loss_limit_counts_realized_and_floating(self):
        self.assertTrue(any("daily loss" in f for f in self.gates(realized_today=-150.0, floating_bot=-60.0)))  # 2.1% of 10k
        self.assertEqual(self.gates(realized_today=-150.0, floating_bot=-40.0), [])

    def test_daily_loss_breach_helper(self):
        self.assertTrue(daily_loss_breached(10000, -200, 0, 2.0))
        self.assertFalse(daily_loss_breached(10000, -199, 0, 2.0))
        self.assertFalse(daily_loss_breached(0, -500, 0, 2.0))

    def test_consecutive_losses_cooldown_and_trade_rate(self):
        self.assertTrue(any("consecutive" in f for f in self.gates(consecutive_losses=3)))
        self.assertTrue(any("cooldown" in f for f in self.gates(last_loss_ts=9_900.0)))  # 100s ago, cooldown 300s
        self.assertEqual(self.gates(last_loss_ts=9_000.0), [])
        self.assertTrue(any("per hour" in f for f in self.gates(trades_last_hour=6)))

    def test_position_count_limits_and_no_hedging_or_reversal(self):
        a = Position(1, "XAUUSD", "BUY", 0.1, 2400.0, 2397.0, 0.0)
        b = Position(2, "BTCUSD", "BUY", 0.1, 60000.0, 59500.0, 0.0)
        self.assertTrue(any("symbol" in f for f in self.gates(open_positions=[a])))
        self.assertTrue(any("total positions" in f for f in self.gates(open_positions=[a, b])))
        sell = Position(3, "XAUUSD", "SELL", 0.1, 2400.0, 2403.0, 0.0)
        self.assertTrue(any("opposite" in f for f in self.gates(open_positions=[sell])))

    def test_netting_account_allows_one_position_per_symbol_only(self):
        self.acct = account(margin_mode=0)
        self.rc["max_positions_per_symbol"] = 3
        a = Position(1, "XAUUSD", "BUY", 0.1, 2400.0, 2397.0, 0.0)
        self.assertTrue(any("netting" in f for f in self.gates(open_positions=[a])))

    def test_aggregate_and_session_exposure_limits(self):
        self.assertTrue(any("aggregate" in f for f in self.gates(open_risk=52.0)))     # 0.75% of 10k = 75; 52 + 24 > 75
        self.assertTrue(any("session exposure" in f for f in self.gates(risk_opened_today=130.0)))  # 1.5% = 150

    def test_margin_gates(self):
        acct = account(margin=2900.0, margin_free=7100.0)
        ri = ok_risk_inputs(acct)
        fails = check_entry(ri, self.rc, "XAUUSD", "XAUUSD", "BUY", 24.0, 500.0, acct, now=0.0)
        self.assertTrue(any("margin utilization" in f for f in fails))  # (2900+500)/10000 = 34% > 30%
        fails = check_entry(ri, self.rc, "XAUUSD", "XAUUSD", "BUY", 24.0, 9000.0, acct, now=0.0)
        self.assertTrue(any("free margin" in f for f in fails))

    def test_trading_lock_blocks_everything(self):
        self.assertTrue(any("locked" in f for f in self.gates(locked=True, lock_reason="daily loss")))


if __name__ == "__main__":
    unittest.main()

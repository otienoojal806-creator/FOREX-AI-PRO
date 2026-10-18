import random
import unittest

from scalper_engine.config import default_config
from scalper_engine.models import Position, Tick
from scalper_engine.trailing_stop_manager import TrailingManager, compute_new_stop

from .fakes import FakeBroker, T0, gold_spec
from .scenarios import Rig

SPEC = gold_spec()
TCFG = dict(default_config()["instruments"]["XAUUSD"]["trail"])
ATR = 2.0


def stop(side, entry, cur_sl, best, bid, ask, cfg=None, spec=SPEC, swing=None):
    return compute_new_stop(side, entry, cur_sl, best, bid, ask, ATR, swing, cfg or TCFG, spec)


class ComputeNewStopTests(unittest.TestCase):
    def test_no_move_before_activation_or_break_even(self):
        # profit 1.0 < break-even 0.8*2=1.6 and < activation 1.0*2=2.0
        self.assertIsNone(stop("BUY", 2400.0, 2397.0, 2401.0, 2400.9, 2401.15))

    def test_break_even_moves_stop_to_entry_plus_offset(self):
        s = stop("BUY", 2400.0, 2397.0, 2401.7, 2401.6, 2401.85)  # profit 1.7 >= 1.6
        self.assertAlmostEqual(s, 2400.05)  # entry + 5 points

    def test_atr_trail_follows_the_high_water_mark(self):
        s = stop("BUY", 2400.0, 2400.05, 2408.0, 2407.9, 2408.15)  # best 2408 - 1 ATR(2.0)
        self.assertAlmostEqual(s, 2406.0)

    def test_sell_trails_the_low_water_mark_from_above(self):
        s = stop("SELL", 2400.0, 2403.0, 2392.0, 2391.9, 2392.15)
        self.assertAlmostEqual(s, 2394.0)  # best 2392 + 2.0

    def test_stop_never_closer_than_broker_stop_level_to_the_market(self):
        cfg = dict(TCFG, distance_atr=0.05, activation_atr=0.5)   # absurdly tight trail
        s = stop("BUY", 2400.0, 2397.0, 2408.0, 2407.9, 2408.15, cfg)
        self.assertLessEqual(s, 2407.9 - SPEC.stops_level * SPEC.point + 1e-9)

    def test_minimum_step_blocks_tiny_improvements(self):
        # current stop 2406.0; new candidate 2406.02 is only 2 points better (min step 5)
        self.assertIsNone(stop("BUY", 2400.0, 2406.0, 2408.02, 2407.95, 2408.2))
        self.assertIsNotNone(stop("BUY", 2400.0, 2406.0, 2408.2, 2408.15, 2408.4))

    def test_freeze_zone_blocks_modification(self):
        spec = gold_spec(freeze_level=1000)  # $10 freeze distance; stop only $2 from the bid
        self.assertIsNone(stop("BUY", 2400.0, 2406.0, 2408.0, 2408.0, 2408.25, spec=spec))

    def test_fixed_distance_mode(self):
        cfg = dict(TCFG, mode="fixed", fixed_points=300, break_even=False)  # $3 trail, activate at $3 profit
        self.assertIsNone(stop("BUY", 2400.0, 2397.0, 2402.9, 2402.8, 2403.05, cfg))
        self.assertAlmostEqual(stop("BUY", 2400.0, 2397.0, 2404.0, 2403.9, 2404.15, cfg), 2401.0)

    def test_swing_mode_uses_structure_with_buffer(self):
        cfg = dict(TCFG, mode="swing", break_even=False)
        s = stop("BUY", 2400.0, 2397.0, 2408.0, 2407.9, 2408.15, cfg, swing=2404.0)
        self.assertAlmostEqual(s, 2404.0 - 0.1 * ATR)

    def test_prices_are_rounded_to_valid_ticks(self):
        spec = gold_spec(tick_size=0.05, point=0.01, digits=2)
        s = stop("BUY", 2400.0, 2397.0, 2408.123, 2408.1, 2408.3, spec=spec)
        self.assertAlmostEqual(s / 0.05, round(s / 0.05), places=6)
        s = stop("SELL", 2400.0, 2403.0, 2391.877, 2391.7, 2391.9, spec=spec)
        self.assertAlmostEqual(s / 0.05, round(s / 0.05), places=6)

    def test_invalid_inputs_return_none(self):
        self.assertIsNone(compute_new_stop("BUY", 2400, 2397, 2408, 0, 2408.2, ATR, None, TCFG, SPEC))
        self.assertIsNone(compute_new_stop("BUY", 2400, 2397, 2408, 2408, 2408.2, 0.0, None, TCFG, SPEC))


class MonotonicityProperty(unittest.TestCase):
    """Whatever the price does, an applied stop only ever tightens and never crosses the market."""

    def simulate(self, side, seed, steps=4000):
        rnd = random.Random(seed)
        entry = 2400.0
        buy = side == "BUY"
        sl = entry - 3.0 if buy else entry + 3.0
        price = entry
        best = entry
        history = [sl]
        for _ in range(steps):
            price += rnd.gauss(0.02 if buy else -0.02, 0.6)
            bid, ask = round(price, 2), round(price + 0.25, 2)
            best = max(best, bid) if buy else min(best, ask)
            new = compute_new_stop(side, entry, sl, best, bid, ask, ATR, None, TCFG, SPEC)
            if new is not None:
                if buy:
                    self.assertGreater(new, sl)
                    self.assertLess(new, bid)
                else:
                    self.assertLess(new, sl)
                    self.assertGreater(new, ask)
                sl = new
                history.append(sl)
            if (buy and bid <= sl) or (not buy and ask >= sl):  # stopped out: path ends
                break
        return history

    def test_buy_stops_only_move_up(self):
        for seed in range(60):
            h = self.simulate("BUY", seed)
            self.assertEqual(h, sorted(h), seed)

    def test_sell_stops_only_move_down(self):
        for seed in range(60):
            h = self.simulate("SELL", seed)
            self.assertEqual(h, sorted(h, reverse=True), seed)


class TrailingManagerTests(unittest.TestCase):
    def build(self):
        self.rig = Rig()
        self.rig.run()  # opens a BUY on the fake broker
        self.b = self.rig.broker
        self.pos = self.b.positions()[0]
        self.persisted = []
        self.alerts = []
        self.now = [1000.0]
        cfg = default_config()
        self.mgr = TrailingManager(
            lambda mode, p, sl: self.rig.ex.modify_stop(self.rig.gw, SPEC, p, sl), lambda s: SPEC,
            lambda s: cfg["instruments"]["XAUUSD"]["trail"], lambda s: ATR, lambda s, side: None,
            lambda st, p: self.persisted.append((st.high_water, st.last_ack_sl)),
            lambda sev, kind, msg, d: self.alerts.append((sev, kind)), clock=lambda: self.now[0])
        self.mgr.register("live", self.pos, ATR, original_sl=self.pos.sl, original_risk=24.0)
        return self.pos

    def tick(self, bid):
        self.now[0] += 1.0
        self.b.set_quote("XAUUSD", bid, round(bid + 0.25, 2))
        p = self.b.positions()[0]
        return self.mgr.on_tick("live", p, self.b.tick("XAUUSD"))

    def test_trailing_moves_the_real_broker_stop_and_verifies_it(self):
        pos = self.build()
        entry, sl0 = pos.price_open, pos.sl
        for bid in (entry + 1.0, entry + 3.0, entry + 6.0, entry + 9.0):
            self.tick(bid)
        now_sl = self.b.pos[pos.ticket].sl
        self.assertGreater(now_sl, sl0)
        self.assertGreaterEqual(now_sl, entry)   # break-even or better after +9
        st = self.mgr.states[("live", pos.ticket)]
        self.assertTrue(st.active)
        self.assertAlmostEqual(st.last_ack_sl, now_sl)
        self.assertAlmostEqual(st.high_water, entry + 9.0)

    def test_retracement_never_loosens_the_stop_and_high_water_holds(self):
        pos = self.build()
        entry = pos.price_open
        for bid in (entry + 5.0, entry + 9.0):
            self.tick(bid)
        tight = self.b.pos[pos.ticket].sl
        for bid in (entry + 7.0, entry + 5.0, entry + 3.5):
            self.tick(bid)
        self.assertGreaterEqual(self.b.pos[pos.ticket].sl, tight)
        self.assertAlmostEqual(self.mgr.states[("live", pos.ticket)].high_water, entry + 9.0)

    def test_state_is_persisted_for_recovery(self):
        pos = self.build()
        self.tick(pos.price_open + 6.0)
        self.assertTrue(self.persisted)
        self.assertGreater(self.persisted[-1][0], pos.price_open)

    def test_restart_restores_water_mark_and_does_not_weaken_protection(self):
        pos = self.build()
        entry = pos.price_open
        for bid in (entry + 4.0, entry + 9.0):
            self.tick(bid)
        stored_high = self.mgr.states[("live", pos.ticket)].high_water
        stored_sl = self.b.pos[pos.ticket].sl

        # "restart": a brand-new manager restored from the persisted row while price has pulled back
        self.b.set_quote("XAUUSD", entry + 5.0, entry + 5.25)
        live = self.b.positions()[0]
        fresh = TrailingManager(
            lambda mode, p, sl: self.rig.ex.modify_stop(self.rig.gw, SPEC, p, sl), lambda s: SPEC,
            lambda s: default_config()["instruments"]["XAUUSD"]["trail"], lambda s: ATR, lambda s, side: None,
            lambda st, p: None, lambda *a: None, clock=lambda: 5000.0)
        st = fresh.register("live", live, ATR, original_sl=pos.sl, original_risk=24.0, high_water=stored_high, active=True)
        self.assertAlmostEqual(st.high_water, stored_high)       # not reset to the (lower) current price
        fresh.on_tick("live", live, self.b.tick("XAUUSD"))
        self.assertGreaterEqual(self.b.pos[pos.ticket].sl, stored_sl)

    def test_failed_modification_is_retried_then_alerts_without_changing_the_stop(self):
        pos = self.build()
        entry, sl0 = pos.price_open, pos.sl
        self.b.sltp_fail = 1000
        for i in range(60):
            self.now[0] += 30.0  # outlast the retry back-off
            self.b.set_quote("XAUUSD", entry + 9.0, entry + 9.25)
            self.mgr.on_tick("live", self.b.positions()[0], self.b.tick("XAUUSD"))
        self.assertEqual(self.b.pos[pos.ticket].sl, sl0)  # unchanged: protection never weakened
        self.assertIn(("critical", "trail_modify_failing"), self.alerts)
        self.assertEqual(self.alerts.count(("critical", "trail_modify_failing")), 1)  # alert once, not every tick

    def test_acknowledged_but_not_applied_stop_is_not_trusted(self):
        pos = self.build()
        self.b.sltp_silent = True  # broker says OK but nothing changes
        self.assertIsNone(self.tick(pos.price_open + 9.0))
        self.assertEqual(self.mgr.states[("live", pos.ticket)].fail_count, 1)


if __name__ == "__main__":
    unittest.main()

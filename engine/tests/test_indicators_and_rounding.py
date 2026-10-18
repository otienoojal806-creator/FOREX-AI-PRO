import math
import random
import unittest

from scalper_engine.indicators import atr, ema, rsi, slope, swing_high, swing_low, true_range
from scalper_engine.models import Bar, floor_to_step, round_price


def naive_rsi(values, period):
    """Independent Wilder RSI used to cross-check the optimised version."""
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


class IndicatorTests(unittest.TestCase):
    def test_ema_seeded_with_sma_and_warmup(self):
        v = [1, 2, 3, 4, 5, 6]
        e = ema(v, 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 2.0)             # SMA seed
        self.assertAlmostEqual(e[3], 4 * 0.5 + 2.0 * 0.5)  # k = 2/(3+1)
        self.assertAlmostEqual(e[5], 5.0)

    def test_ema_of_constant_is_constant(self):
        self.assertTrue(all(abs(x - 7.5) < 1e-12 for x in ema([7.5] * 40, 9)[8:]))

    def test_ema_short_input_is_all_none(self):
        self.assertEqual(ema([1, 2], 5), [None, None])

    def test_rsi_extremes(self):
        up = rsi([float(i) for i in range(1, 40)], 14)
        down = rsi([float(i) for i in range(40, 1, -1)], 14)
        flat = rsi([5.0] * 30, 14)
        self.assertEqual(up[-1], 100.0)
        self.assertAlmostEqual(down[-1], 0.0)
        self.assertEqual(flat[-1], 50.0)

    def test_rsi_matches_independent_implementation_and_is_bounded(self):
        rnd = random.Random(3)
        v = [100.0]
        for _ in range(200):
            v.append(v[-1] + rnd.uniform(-1, 1))
        out = rsi(v, 14)
        self.assertAlmostEqual(out[-1], naive_rsi(v, 14), places=9)
        self.assertTrue(all(0.0 <= x <= 100.0 for x in out if x is not None))

    def test_atr_of_constant_range_bars(self):
        bars = [Bar(i * 60, 10, 12, 8, 10) for i in range(40)]
        self.assertAlmostEqual(atr(bars, 14)[-1], 4.0)

    def test_true_range_uses_gap_against_previous_close(self):
        bars = [Bar(0, 10, 11, 9, 10), Bar(60, 15, 16, 14, 15)]
        self.assertEqual(true_range(bars), [2, 6])  # |16 - 10|

    def test_swing_levels_and_slope(self):
        bars = [Bar(i * 60, 10, 10 + i, 10 - i, 10) for i in range(5)]
        self.assertEqual(swing_high(bars, 3), 14)
        self.assertEqual(swing_low(bars, 3), 6)
        self.assertAlmostEqual(slope([1.0, 2.0, 3.0, 4.0, 5.0], 3), 3.0)


class RoundingTests(unittest.TestCase):
    def test_floor_to_step_is_exact_and_never_rounds_up(self):
        self.assertEqual(floor_to_step(0.07, 0.01), 0.07)
        self.assertEqual(floor_to_step(0.0799, 0.01), 0.07)
        self.assertEqual(floor_to_step(0.06999999999999999, 0.01), 0.07)  # float noise, not a real shortfall
        self.assertEqual(floor_to_step(0.0099, 0.01), 0.0)
        self.assertEqual(floor_to_step(1.37, 0.05), 1.35)

    def test_floor_to_step_property(self):
        rnd = random.Random(1)
        for _ in range(2000):
            step = rnd.choice([0.01, 0.02, 0.05, 0.1, 1.0])
            x = rnd.uniform(0, 5)
            r = floor_to_step(x, step)
            self.assertLessEqual(r, x + 1e-9)
            self.assertGreater(r, x - step - 1e-9)

    def test_round_price_modes(self):
        self.assertEqual(round_price(2400.126, 0.01, 2, "down"), 2400.12)
        self.assertEqual(round_price(2400.121, 0.01, 2, "up"), 2400.13)
        self.assertEqual(round_price(2400.125, 0.01, 2, "nearest"), 2400.13)
        self.assertEqual(round_price(100.07, 0.05, 2, "down"), 100.05)
        self.assertEqual(round_price(100.07, 0.05, 2, "up"), 100.10)

    def test_round_price_result_is_on_the_tick_grid(self):
        rnd = random.Random(2)
        for _ in range(1000):
            tick = rnd.choice([0.01, 0.05, 0.5])
            p = rnd.uniform(1000, 70000)
            for mode in ("down", "up", "nearest"):
                r = round_price(p, tick, 2, mode)
                self.assertAlmostEqual(r / tick, round(r / tick), places=6)
        self.assertFalse(math.isnan(round_price(1.0, 0.0, 2)))


if __name__ == "__main__":
    unittest.main()

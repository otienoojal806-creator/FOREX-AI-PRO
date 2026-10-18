import copy
import unittest
from dataclasses import replace

from scalper_engine.config import default_config, instrument_strategy
from scalper_engine.models import TF_M1, TF_M5, Bar, Tick
from scalper_engine.strategy_engine import evaluate, make_signal_id

from .fakes import FakeBroker, T0, aggregate, make_bars
from .scenarios import BUY_SEED, LAST_BAR, SELL_SEED, analysis_for, load_market


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = default_config()
        self.b = FakeBroker()
        load_market(self.b)

    def run_eval(self, m1=None, m5=None, tick=None, tick_age=2.0, cfg=None, symbol="XAUUSD"):
        cfg = cfg or self.cfg
        inst = "XAUUSD" if symbol == "XAUUSD" else "BTCUSD"
        return evaluate(inst, symbol, m1 or self.b.bars[(symbol, TF_M1)], m5 or self.b.bars[(symbol, TF_M5)], None,
                        tick or self.b.ticks[symbol], self.b.specs[symbol], instrument_strategy(cfg, inst),
                        cfg["instruments"][inst], tick_age, cfg["risk"]["min_net_rr"])

    # ---- a valid BUY ----
    def test_valid_buy_has_complete_structured_analysis(self):
        a = self.run_eval()
        self.assertEqual(a.decision, "BUY", a.reject_reasons)
        d = a.to_dict()
        for key in ("instrument", "broker_symbol", "decision", "signal_id", "bid", "ask", "trend", "momentum", "indicators",
                    "sl_price", "tp_price", "rr_net", "costs", "strategy_version", "rationale", "expires_at", "invalidation"):
            self.assertIn(key, d)
            self.assertTrue(d[key] not in (None, "", [], {}), key)
        for ind in ("ema9", "ema21", "ema50", "rsi14", "atr14"):
            self.assertIn(ind, a.indicators)
        entry = a.ask
        self.assertLess(a.sl_price, entry)
        self.assertGreater(a.tp_price, entry)
        self.assertGreaterEqual(a.rr_net, self.cfg["risk"]["min_net_rr"])
        self.assertEqual(a.trend["m5"], "BULLISH")
        self.assertEqual(a.bar_time, LAST_BAR)  # evaluated the last COMPLETED candle

    def test_valid_sell_is_the_mirror_image(self):
        load_market(self.b, kind=SELL_SEED)
        a = self.run_eval()
        self.assertEqual(a.decision, "SELL", a.reject_reasons)
        self.assertGreater(a.sl_price, a.bid)
        self.assertLess(a.tp_price, a.bid)
        self.assertEqual(a.trend["m5"], "BEARISH")

    def test_prices_are_on_the_tick_grid(self):
        a = self.run_eval()
        for p in (a.sl_price, a.tp_price):
            self.assertAlmostEqual(p / 0.01, round(p / 0.01), places=6)

    # ---- deduplication ----
    def test_signal_id_is_deterministic_and_unique_per_candle_and_direction(self):
        a1, a2 = self.run_eval(), self.run_eval()
        self.assertEqual(a1.signal_id, a2.signal_id)
        self.assertEqual(a1.signal_id, make_signal_id("XAUUSD", a1.bar_time, a1.strategy_version, "BUY"))
        self.assertNotEqual(a1.signal_id, make_signal_id("XAUUSD", a1.bar_time + 60, a1.strategy_version, "BUY"))
        self.assertNotEqual(a1.signal_id, make_signal_id("XAUUSD", a1.bar_time, a1.strategy_version, "SELL"))
        self.assertNotEqual(a1.signal_id, make_signal_id("BTCUSD", a1.bar_time, a1.strategy_version, "BUY"))
        self.assertNotEqual(a1.signal_id, make_signal_id("XAUUSD", a1.bar_time, "other-version", "BUY"))

    def test_signal_expires_shortly_after_the_candle_closes(self):
        a = self.run_eval()
        self.assertEqual(a.expires_at, LAST_BAR + 60 + self.cfg["strategy"]["signal_ttl_sec"])

    # ---- NO TRADE paths ----
    def assert_no_trade(self, a, fragment):
        self.assertEqual(a.decision, "NO TRADE")
        self.assertEqual(a.signal_id, "")
        self.assertTrue(any(fragment in r for r in a.reject_reasons), a.reject_reasons)

    def test_insufficient_history(self):
        self.assert_no_trade(self.run_eval(m1=self.b.bars[(("XAUUSD"), TF_M1)][-30:]), "not enough candle history")

    def test_wide_spread_is_rejected(self):
        t = self.b.ticks["XAUUSD"]
        a = self.run_eval(tick=Tick(t.time_msc, t.bid - 0.5, t.ask))  # +50 points
        self.assert_no_trade(a, "spread")

    def test_stale_quote_is_rejected(self):
        self.assert_no_trade(self.run_eval(tick_age=60.0), "stale quote")

    def test_quote_far_ahead_of_last_candle_means_candle_data_is_stale(self):
        t = self.b.ticks["XAUUSD"]
        self.assert_no_trade(self.run_eval(tick=Tick(t.time_msc + 600_000, t.bid, t.ask)), "candle is stale")

    def test_invalid_quote_is_rejected(self):
        self.assert_no_trade(self.run_eval(tick=Tick(self.b.ticks["XAUUSD"].time_msc, 2400.5, 2400.0)), "invalid quote")

    def test_malformed_candle_is_rejected(self):
        m1 = list(self.b.bars[("XAUUSD", TF_M1)])
        m1[-2] = Bar(m1[-2].time, 2400, 2399, 2401, 2400)  # high < low
        self.assert_no_trade(self.run_eval(m1=m1), "malformed")

    def test_conflicting_timeframes_do_not_trade(self):
        up = make_bars(600, 2300.0, 0.10, 1.6, LAST_BAR, seed=9)
        down = make_bars(600, 2400.0, -0.20, 1.6, LAST_BAR, seed=4)
        a = self.run_eval(m1=down, m5=aggregate(up[:595]), tick=Tick((down[-1].time + 65) * 1000, down[-1].close - 0.1, down[-1].close + 0.15))
        self.assertEqual(a.decision, "NO TRADE")
        self.assertTrue(any("conflicts" in r or "confirm" in r for r in a.reject_reasons), a.reject_reasons)

    def test_sideways_market_has_no_confirmed_trend(self):
        flat = make_bars(600, 2400.0, 0.0, 1.0, LAST_BAR, seed=11)
        a = self.run_eval(m1=flat, m5=aggregate(flat[:595]), tick=Tick((flat[-1].time + 65) * 1000, flat[-1].close - 0.1, flat[-1].close + 0.15))
        self.assertEqual(a.decision, "NO TRADE")

    def test_reward_must_survive_costs(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["instruments"]["XAUUSD"]["slippage_points"] = 400   # $4 of assumed slippage kills the R:R
        a = self.run_eval(cfg=cfg)
        self.assert_no_trade(a, "after costs")

    def test_volatility_spike_is_rejected(self):
        m1 = list(self.b.bars[("XAUUSD", TF_M1)])
        for i in range(-3, 0):  # three violent candles right before the signal
            b = m1[i]
            m1[i] = replace(b, high=b.high + 9.0, low=b.low - 9.0)
        a = self.run_eval(m1=m1)
        self.assertEqual(a.decision, "NO TRADE")
        self.assertTrue(any("volatility" in r or "wide" in r for r in a.reject_reasons), a.reject_reasons)

    def test_no_trade_always_carries_reasons(self):
        a = self.run_eval(tick_age=999)
        self.assertTrue(a.rationale)

    def test_instrument_overrides_apply_independently(self):
        s_gold = instrument_strategy(self.cfg, "XAUUSD")
        s_btc = instrument_strategy(self.cfg, "BTCUSD")
        self.assertNotEqual(s_gold["sl_atr_mult"], s_btc["sl_atr_mult"])
        self.assertEqual(self.cfg["strategy"]["sl_atr_mult"], 1.3)  # global value untouched by the merge


if __name__ == "__main__":
    unittest.main()

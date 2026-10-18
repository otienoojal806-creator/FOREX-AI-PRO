import unittest

from scalper_engine.models import SymbolSpec, Tick
from scalper_engine.order_executor import filling_candidates

from .fakes import gold_spec
from .scenarios import Rig, analysis_for


class EntryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()

    # ---------- happy path ----------
    def test_filled_order_is_verified_and_protected(self):
        res = self.r.run()
        self.assertTrue(res.ok)
        self.assertEqual(res.state, "PROTECTED")
        pos = self.r.broker.pos[res.ticket]
        self.assertGreater(pos.sl, 0)
        self.assertLess(pos.sl, pos.price_open)       # BUY stop below entry
        self.assertGreater(pos.tp, pos.price_open)
        self.assertEqual(res.protective_state, "OK")
        row = self.r.journal.get_execution(self.r.analysis.signal_id, "live")
        self.assertEqual(row["state"], "PROTECTED")
        self.assertEqual(row["position_ticket"], res.ticket)
        self.assertAlmostEqual(row["fill_price"], pos.price_open)

    def test_request_carries_stop_target_magic_comment_and_checks_ran_first(self):
        self.r.run()
        req = self.r.opens()[0]
        self.assertGreater(req["sl"], 0)
        self.assertGreater(req["tp"], 0)
        self.assertEqual(req["magic"], self.r.cfg["execution"]["magic"])
        self.assertTrue(req["comment"].startswith("FAIP"))
        self.assertEqual(len(self.r.broker.checks), 1)
        self.assertEqual(self.r.broker.checks[0]["sl"], req["sl"])

    def test_actual_risk_never_exceeds_the_configured_percentage(self):
        res = self.r.run()
        acct = self.r.broker.acct
        loss = res.filled_volume * abs(res.fill_price - self.r.broker.pos[res.ticket].sl) * 100
        self.assertLessEqual(loss, acct.equity * self.r.cfg["risk"]["risk_per_trade_pct"] / 100 + 0.01)

    # ---------- authorization and preconditions ----------
    def test_live_orders_need_explicit_authorization(self):
        self.r.authorized = False
        res = self.r.run()
        self.assertFalse(res.ok)
        self.assertIn("not authorized", res.reason)
        self.assertEqual(self.r.broker.sent, [])

    def test_wrong_account_is_refused(self):
        self.r.expected_login = 999
        res = self.r.run()
        self.assertFalse(res.ok)
        self.assertIn("does not match", res.reason)
        self.assertEqual(self.r.broker.sent, [])

    def test_terminal_trading_disabled_is_refused(self):
        self.r.broker.term = type(self.r.broker.term)(True, False)
        self.assertIn("not permitted", self.r.run().reason)
        self.assertEqual(self.r.broker.sent, [])

    def test_disconnected_terminal_is_refused(self):
        self.r.broker.connected = False
        self.assertIn("not connected", self.r.run().reason)

    def test_symbol_not_open_for_trading_is_refused(self):
        self.r.broker.specs["XAUUSD"] = gold_spec(trade_mode=3)  # close-only
        self.assertIn("not open", self.r.run().reason)

    def test_spread_gate_at_execution_time(self):
        t = self.r.broker.ticks["XAUUSD"]
        self.r.broker.ticks["XAUUSD"] = Tick(t.time_msc, t.bid - 1.0, t.ask)
        self.assertIn("spread", self.r.run().reason)

    def test_expired_signal_is_not_traded(self):
        t = self.r.broker.ticks["XAUUSD"]
        self.r.broker.ticks["XAUUSD"] = Tick(t.time_msc + 60_000, t.bid, t.ask)  # a minute later
        res = self.r.run()
        self.assertEqual(res.reason, "signal expired")
        self.assertEqual(self.r.broker.sent, [])

    def test_price_moving_against_the_signal_beyond_slippage_is_refused(self):
        t = self.r.broker.ticks["XAUUSD"]
        self.r.broker.ticks["XAUUSD"] = Tick(t.time_msc, t.bid + 1.0, t.ask + 1.0)  # +$1 against an 8 point (=$0.08) allowance
        res = self.r.run()
        self.assertIn("slippage", res.reason)
        self.assertEqual(self.r.broker.sent, [])

    def test_non_actionable_signal_is_never_executed(self):
        a = analysis_for(self.r.broker)
        a.decision = "NO TRADE"
        self.assertFalse(self.r.run(a).ok)
        self.assertEqual(self.r.broker.sent, [])

    # ---------- duplicate protection ----------
    def test_the_same_signal_cannot_be_executed_twice(self):
        first = self.r.run()
        second = self.r.run()
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.reason, "signal already processed")
        self.assertEqual(len(self.r.opens()), 1)
        self.assertEqual(len(self.r.broker.pos), 1)

    # ---------- risk gates ----------
    def test_risk_gate_failure_blocks_the_order(self):
        self.r.risk_kw = {"consecutive_losses": 3}
        res = self.r.run()
        self.assertFalse(res.ok)
        self.assertIn("consecutive", res.reason)
        self.assertEqual(self.r.broker.sent, [])

    def test_insufficient_free_margin_blocks_the_order(self):
        self.r.broker.acct = type(self.r.broker.acct)(**{**vars(self.r.broker.acct), "margin_free": 1.0})
        res = self.r.run()
        self.assertIn("free margin", res.reason)

    def test_minimum_lot_too_risky_blocks_the_order(self):
        self.r.broker.acct = type(self.r.broker.acct)(**{**vars(self.r.broker.acct), "equity": 300.0, "balance": 300.0, "margin_free": 300.0})
        res = self.r.run()
        self.assertFalse(res.ok)
        self.assertIn("minimum volume", res.reason)
        self.assertEqual(self.r.broker.sent, [])

    # ---------- broker validation ----------
    def test_order_check_failure_means_no_order_is_sent(self):
        self.r.broker.check_retcode = 10019  # no money
        res = self.r.run()
        self.assertIn("order_check failed", res.reason)
        self.assertEqual(self.r.opens(), [])

    def test_filling_policy_falls_back_when_the_first_is_invalid(self):
        self.r.broker.check_script = [10030, 0]  # invalid fill, then OK
        res = self.r.run()
        self.assertTrue(res.ok)
        self.assertEqual(len(self.r.broker.checks), 2)
        self.assertNotEqual(self.r.broker.checks[0]["type_filling"], self.r.broker.checks[1]["type_filling"])

    def test_filling_candidates_follow_symbol_flags(self):
        both = gold_spec(filling_mode=3)
        self.assertEqual(filling_candidates(both)[-1], 2)  # RETURN is always the last resort
        self.assertEqual(len(filling_candidates(gold_spec(filling_mode=0))), 1)

    # ---------- rejections and requotes ----------
    def test_broker_rejection_is_recorded_and_not_resent(self):
        self.r.broker.open_script = ["reject:10018"]
        res = self.r.run()
        self.assertEqual(res.state, "REJECTED")
        self.assertEqual(len(self.r.opens()), 1)
        self.assertEqual(self.r.broker.pos, {})
        self.assertEqual(self.r.journal.get_execution(self.r.analysis.signal_id, "live")["state"], "REJECTED")
        self.assertTrue(any(e["kind"] == "order_rejected" for e in self.r.journal.events))  # market closed -> operator warning

    def test_requote_is_retried_once_at_the_fresh_price_and_can_succeed(self):
        self.r.broker.open_script = ["reject:10004", "ok"]
        res = self.r.run()
        self.assertTrue(res.ok)
        self.assertEqual(len(self.r.opens()), 2)

    def test_requotes_are_bounded(self):
        self.r.broker.open_script = ["reject:10004"] * 10
        res = self.r.run()
        self.assertFalse(res.ok)
        self.assertEqual(len(self.r.opens()), 1 + self.r.cfg["execution"]["max_retries"])
        self.assertEqual(self.r.broker.pos, {})

    # ---------- partial fills ----------
    def test_partial_fill_is_protected_with_the_filled_volume(self):
        self.r.broker.open_script = ["partial"]
        res = self.r.run()
        self.assertTrue(res.ok)
        self.assertTrue(res.detail["partial"])
        requested = self.r.opens()[0]["volume"]
        self.assertLess(res.filled_volume, requested)
        self.assertGreater(self.r.broker.pos[res.ticket].sl, 0)
        self.assertTrue(any(e["kind"] == "partial_fill" for e in self.r.journal.events))

    # ---------- ambiguous outcomes ----------
    def test_timeout_after_fill_is_reconciled_not_resent(self):
        self.r.broker.open_script = ["timeout_filled"]
        res = self.r.run()
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(len(self.r.opens()), 1)        # NEVER resent
        self.assertEqual(len(self.r.broker.pos), 1)     # exactly one position
        self.assertGreater(self.r.broker.pos[res.ticket].sl, 0)
        self.assertTrue(any(e["kind"] == "ambiguous_resolved" for e in self.r.journal.events))

    def test_timeout_with_no_fill_stays_ambiguous_and_raises_a_critical_alert(self):
        self.r.broker.open_script = ["timeout_lost"]
        res = self.r.run()
        self.assertEqual(res.state, "AMBIGUOUS")
        self.assertFalse(res.ok)
        self.assertEqual(len(self.r.opens()), 1)        # still never resent
        self.assertTrue(any(e["severity"] == "critical" and e["kind"] == "ambiguous_order" for e in self.r.journal.events))
        self.assertEqual(self.r.journal.get_execution(self.r.analysis.signal_id, "live")["state"], "AMBIGUOUS")

    def test_empty_terminal_reply_is_treated_as_ambiguous(self):
        self.r.broker.open_script = ["none"]
        self.assertEqual(self.r.run().state, "AMBIGUOUS")
        self.assertEqual(len(self.r.opens()), 1)

    # ---------- missing protective stop ----------
    def test_fill_without_a_stop_is_repaired(self):
        self.r.broker.open_script = ["no_stop"]
        res = self.r.run()
        self.assertTrue(res.ok)
        self.assertEqual(res.protective_state, "RECOVERED")
        pos = self.r.broker.pos[res.ticket]
        self.assertGreater(pos.sl, 0)
        self.assertLess(pos.sl, self.r.broker.ticks["XAUUSD"].bid)
        self.assertTrue(any(e["kind"] == "missing_stop" and e["severity"] == "critical" for e in self.r.journal.events))

    def test_exposure_is_closed_when_a_stop_cannot_be_set(self):
        self.r.broker.open_script = ["no_stop"]
        self.r.broker.sltp_fail = 1000
        res = self.r.run()
        self.assertFalse(res.ok)
        self.assertEqual(res.state, "CLOSED_UNPROTECTED")
        self.assertEqual(self.r.broker.pos, {})          # flat, verified with the broker
        self.assertEqual(self.r.journal.get_execution(self.r.analysis.signal_id, "live")["protective_state"], "CLOSED_UNPROTECTED")
        self.assertTrue(any(e["kind"] == "closed_unprotected" for e in self.r.journal.events))

    def test_unprotected_position_that_cannot_be_closed_is_escalated_never_hidden(self):
        self.r.broker.open_script = ["no_stop"]
        self.r.broker.sltp_fail = 1000
        self.r.broker.close_fail = 1000
        res = self.r.run()
        self.assertEqual(res.state, "UNPROTECTED_OPEN")
        self.assertEqual(len(self.r.broker.pos), 1)
        self.assertTrue(any(e["kind"] == "unprotected_open" and e["severity"] == "critical" for e in self.r.journal.events))
        # and no hedge or reversal was attempted: the only orders are the open and the closing attempts of the SAME position
        self.assertEqual(len(self.r.opens()), 1)

    # ---------- durability ----------
    def test_no_durable_intent_means_no_order(self):
        self.r.journal.fail_writes = True
        res = self.r.run()
        self.assertFalse(res.ok)
        self.assertIn("persist", res.reason)
        self.assertEqual(self.r.broker.sent, [])


class StopModificationTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        res = self.r.run()
        self.pos = self.r.broker.positions()[0]
        self.spec = self.r.broker.specs["XAUUSD"]

    def test_modify_tightens_and_returns_the_broker_confirmed_stop(self):
        new = round(self.pos.price_open + 0.5, 2)
        self.assertEqual(self.r.ex.modify_stop(self.r.gw, self.spec, self.pos, new), new)
        self.assertEqual(self.r.broker.pos[self.pos.ticket].sl, new)

    def test_modify_refuses_to_loosen(self):
        before = self.r.broker.pos[self.pos.ticket].sl
        self.assertIsNone(self.r.ex.modify_stop(self.r.gw, self.spec, self.pos, before - 1.0))
        self.assertIsNone(self.r.ex.modify_stop(self.r.gw, self.spec, self.pos, before))
        self.assertEqual(self.r.broker.pos[self.pos.ticket].sl, before)
        self.assertEqual([x for x in self.r.broker.sent if x["action"] == 6], [])  # nothing was even sent

    def test_modify_reports_failure_when_broker_rejects(self):
        self.r.broker.sltp_fail = 1
        self.assertIsNone(self.r.ex.modify_stop(self.r.gw, self.spec, self.pos, self.pos.price_open + 0.5))

    def test_close_position_is_verified(self):
        self.assertTrue(self.r.ex.close_position(self.r.gw, self.pos, "test"))
        self.assertEqual(self.r.broker.pos, {})

    def test_close_failure_is_reported_honestly(self):
        self.r.broker.close_fail = 100
        self.assertFalse(self.r.ex.close_position(self.r.gw, self.pos, "test"))
        self.assertEqual(len(self.r.broker.pos), 1)


if __name__ == "__main__":
    unittest.main()

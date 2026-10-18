import json
import time
import unittest
import urllib.error
import urllib.request

from scalper_engine.api import make_server
from scalper_engine.config import EnvSettings
from scalper_engine.engine import Engine
from scalper_engine.models import DEAL_REASON_SL
from scalper_engine.monitoring_and_alerts import Alerts
from scalper_engine.trade_journal import MemoryJournal, now_iso

from .fakes import FakeBroker, T0, account
from .scenarios import load_market

STORED_CONFIG = {"instruments": {"BTCUSD": {"enabled": False}, "XAUUSD": {"sessions_utc": []}}}


def make_env(**kw):
    d = dict(supabase_url="https://x.supabase.co", service_key="svc-key-123456", user_id="u" * 36, mt5_login=1234567,
             api_token="tok-abcdef-123456")
    d.update(kw)
    return EnvSettings(**d)


class Rig:
    """One 'VPS process': an Engine over a shared broker and journal. Build another Rig on the same objects to simulate a restart."""

    def __init__(self, broker=None, journal=None, **env_kw):
        self.broker = broker or FakeBroker()
        if broker is None:
            load_market(self.broker)
        self.journal = journal or MemoryJournal()
        if journal is None:
            self.journal.config = STORED_CONFIG
        self.alerts = Alerts(self.journal, dedupe_sec=0)
        self.eng = Engine(make_env(**env_kw), self.broker, self.journal, self.alerts, sleep=lambda s: None)

    def start(self):
        self.eng.startup()
        return self

    def cmd(self, name, **payload):
        return self.eng.handle_command(name, payload)

    def go_live(self):
        """Walk the real arming flow: preview -> arm -> resume."""
        st, pv = self.cmd("request_live_preview")
        assert st == "done", pv
        st, r = self.cmd("arm_live", preview_token=pv["preview_token"], confirm_text="ARM LIVE")
        assert st == "done", r
        st, r = self.cmd("resume_entries")
        assert st == "done", r

    def new_bar(self):
        """Advance the market by one completed candle so the next analysis pass sees a fresh bar."""
        from scalper_engine.models import Bar, TF_M1
        bars = self.broker.bars[("XAUUSD", TF_M1)]
        last = bars[-1]
        self.broker.bars[("XAUUSD", TF_M1)] = bars + [Bar(last.time + 60, last.close, last.close + 0.2, last.close - 0.2, last.close + 0.05, 100, 25)]


class SafeStartTests(unittest.TestCase):
    def test_engine_always_starts_paused_disarmed_and_idle(self):
        r = Rig().start()
        e = r.eng
        self.assertTrue(e.connected)
        self.assertTrue(e.paused)
        self.assertFalse(e.live_armed)
        self.assertEqual(e.exec_mode, "none")
        self.assertFalse(e.analysis_running)
        self.assertEqual(e.compute_mode(), "PAUSED")
        self.assertEqual(r.journal.state["mode"], "PAUSED")
        self.assertFalse(r.journal.state["entries_enabled"])

    def test_restart_after_live_trading_does_not_resume_live(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        self.assertEqual(r.eng.compute_mode(), "LIVE ACTIVE")
        r2 = Rig(r.broker, r.journal, allow_live=True).start()   # process restarted (crash, deploy, VPS reboot)
        self.assertEqual(r2.eng.compute_mode(), "PAUSED")
        self.assertTrue(r2.eng.paused)
        self.assertFalse(r2.eng.live_armed)
        self.assertEqual(r2.eng.exec_mode, "none")
        r2.eng.analysis_pass()  # and it cannot place orders until a person arms it again
        self.assertEqual([x for x in r2.broker.sent if x["action"] == 1], [x for x in r.broker.sent if x["action"] == 1])

    def test_commands_queued_before_a_restart_are_discarded_not_replayed(self):
        j = MemoryJournal()
        j.config = STORED_CONFIG
        j.commands = [{"id": "a", "command": "arm_live", "payload": {}, "status": "pending", "ts": time.time()},
                      {"id": "b", "command": "resume_entries", "payload": {}, "status": "processing", "ts": time.time()}]
        r = Rig(journal=j, allow_live=True).start()
        r.eng.command_pass()
        self.assertEqual([c["status"] for c in j.commands], ["rejected", "rejected"])
        self.assertEqual(r.eng.compute_mode(), "PAUSED")
        self.assertTrue(any(a["action"] == "engine.start" for a in j.audits))

    def test_wrong_account_refuses_to_connect(self):
        b = FakeBroker()
        load_market(b)
        b.acct = account(login=999)
        r = Rig(b)
        ok, msg = r.eng.connect()
        self.assertFalse(ok)
        self.assertIn("does not match", msg)
        self.assertEqual(r.eng.compute_mode(), "DISCONNECTED")
        self.assertTrue(any(e["kind"] == "account_mismatch" for e in r.journal.events))

    def test_commands_that_need_the_terminal_are_rejected_when_disconnected(self):
        b = FakeBroker()
        b.connect_ok = False
        r = Rig(b).start()
        st, res = r.cmd("start_paper")
        self.assertEqual(st, "rejected")
        self.assertEqual(r.eng.compute_mode(), "DISCONNECTED")

    def test_only_verified_symbols_are_traded(self):
        b = FakeBroker()
        load_market(b)
        j = MemoryJournal()
        j.config = {"instruments": {"BTCUSD": {"enabled": False}, "XAUUSD": {"broker_symbol": "GOLD.nope", "sessions_utc": []}}}
        r = Rig(b, j).start()
        self.assertEqual(r.eng.sym_of, {})
        self.assertEqual(r.cmd("start_paper")[0], "rejected")
        self.assertTrue(any(e["kind"] == "symbol_mapping" for e in j.events))


class PaperTradingTests(unittest.TestCase):
    def test_paper_mode_trades_real_quotes_without_ever_touching_the_broker_order_api(self):
        r = Rig().start()
        self.assertEqual(r.cmd("start_paper")[0], "done")
        self.assertEqual(r.eng.compute_mode(), "PAPER")
        r.eng.analysis_pass()
        sig = list(r.journal.signals.values())
        self.assertEqual([s["decision"] for s in sig], ["BUY"])
        self.assertEqual(sig[0]["outcome"], "paper_executed")
        self.assertEqual(r.broker.sent, [])                      # nothing was sent to the real terminal
        self.assertEqual(len(r.eng.paper_gw.positions()), 1)
        row = r.journal.positions("paper", "open")[0]
        self.assertEqual(row["mode"], "paper")
        self.assertGreater(row["sl"], 0)
        self.assertEqual(r.eng.live_gw.positions(), [])

    def test_one_entry_per_candle_even_across_repeated_passes_and_restarts(self):
        r = Rig().start()
        r.cmd("start_paper")
        for _ in range(3):
            r.eng.analysis_pass()
        self.assertEqual(len(r.journal.signals), 1)
        self.assertEqual(len(r.eng.paper_gw.positions()), 1)
        r2 = Rig(r.broker, r.journal).start()                    # restart, same candle still the latest
        r2.cmd("start_paper")
        r2.eng.analysis_pass()
        self.assertEqual(len(r2.journal.signals), 1)             # signal id already stored -> no second entry
        self.assertEqual(len(r2.eng.paper_gw.positions()), 1)    # restored from the journal, not duplicated

    def test_paused_engine_records_the_signal_but_never_enters(self):
        r = Rig().start()
        r.cmd("start_analysis")
        r.eng.analysis_pass()
        s = list(r.journal.signals.values())[0]
        self.assertEqual(s["decision"], "BUY")
        self.assertNotEqual(s["outcome"], "paper_executed")
        self.assertEqual(r.eng.paper_gw.positions(), [])

    def test_pause_resume_commands(self):
        r = Rig().start()
        r.cmd("start_paper")
        self.assertEqual(r.cmd("pause_entries")[0], "done")
        self.assertEqual(r.eng.compute_mode(), "PAUSED")
        r.eng.analysis_pass()
        self.assertEqual(r.eng.paper_gw.positions(), [])
        self.assertEqual(r.cmd("resume_entries")[0], "done")
        self.assertEqual(r.eng.compute_mode(), "PAPER")


class LiveGatingTests(unittest.TestCase):
    def test_live_is_impossible_when_the_vps_switch_is_off(self):
        r = Rig(allow_live=False).start()
        st, res = r.cmd("request_live_preview")
        self.assertEqual(st, "rejected")
        self.assertIsNone(res["preview_token"])
        self.assertTrue(any(not c["ok"] and "ENGINE_ALLOW_LIVE" in c["name"] for c in res["checks"]))
        self.assertEqual(r.cmd("arm_live", preview_token="guess", confirm_text="ARM LIVE")[0], "rejected")
        # even forcing the internal flag cannot produce an order
        r.eng.live_armed, r.eng.exec_mode, r.eng.paused, r.eng.analysis_running = True, "live", False, True
        r.eng.analysis_pass()
        self.assertEqual([x for x in r.broker.sent if x["action"] == 1], [])
        self.assertNotEqual(r.eng.compute_mode(), "LIVE ACTIVE")

    def test_preview_shows_the_account_and_limits_the_user_must_confirm(self):
        r = Rig(allow_live=True).start()
        st, pv = r.cmd("request_live_preview")
        self.assertEqual(st, "done", [c for c in pv["checks"] if not c["ok"]])
        self.assertEqual(pv["account"]["login"], 1234567)
        self.assertEqual(pv["account"]["type"], "DEMO")
        self.assertIn("XAUUSD", pv["instruments"])
        self.assertIn("risk_per_trade_pct", pv["risk"])
        self.assertIn("lose money", pv["warning"])
        self.assertTrue(pv["preview_token"])

    def test_arming_needs_a_valid_unexpired_token_and_leaves_entries_paused(self):
        r = Rig(allow_live=True).start()
        st, pv = r.cmd("request_live_preview")
        self.assertEqual(r.cmd("arm_live", preview_token="wrong")[0], "rejected")
        r.cmd("request_live_preview")                              # a bad attempt burns the previous token
        self.assertEqual(r.cmd("arm_live", preview_token=pv["preview_token"])[0], "rejected")
        st, pv = r.cmd("request_live_preview")
        r.eng._clock = lambda: time.time() + 500                   # token expires after 120 s
        self.assertEqual(r.cmd("arm_live", preview_token=pv["preview_token"])[0], "rejected")
        r.eng._clock = time.time
        st, pv = r.cmd("request_live_preview")
        self.assertEqual(r.cmd("arm_live", preview_token=pv["preview_token"])[0], "done")
        self.assertEqual(r.eng.compute_mode(), "LIVE ARMED")       # armed, but not trading yet
        self.assertTrue(r.eng.paused)
        self.assertTrue(any(a["action"] == "live.armed" for a in r.journal.audits))

    def test_real_accounts_need_a_second_switch(self):
        b = FakeBroker()
        load_market(b)
        b.acct = account(trade_mode=2)
        r = Rig(b, allow_live=True).start()
        st, pv = r.cmd("request_live_preview")
        self.assertEqual(st, "rejected")
        self.assertTrue(any("REAL account" in c["detail"] for c in pv["checks"]))
        r2 = Rig(b, allow_live=True, allow_real_account=True).start()
        self.assertEqual(r2.cmd("request_live_preview")[0], "done")

    def test_live_entry_flow_places_a_verified_protected_order(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        self.assertEqual(r.eng.compute_mode(), "LIVE ACTIVE")
        r.eng.analysis_pass()
        opens = [x for x in r.broker.sent if x["action"] == 1 and "position" not in x]
        self.assertEqual(len(opens), 1)
        self.assertEqual(len(r.broker.pos), 1)
        pos = next(iter(r.broker.pos.values()))
        self.assertGreater(pos.sl, 0)
        sig = list(r.journal.signals.values())[0]
        self.assertEqual(sig["outcome"], "live_executed")
        self.assertEqual(r.journal.positions("live", "open")[0]["ticket"], pos.ticket)

    def test_resume_is_refused_when_live_health_checks_fail(self):
        r = Rig(allow_live=True).start()
        st, pv = r.cmd("request_live_preview")
        r.cmd("arm_live", preview_token=pv["preview_token"])
        r.broker.term = type(r.broker.term)(True, False)           # operator switched Algo Trading off in the terminal
        st, res = r.cmd("resume_entries")
        self.assertEqual(st, "rejected")
        self.assertEqual(r.eng.compute_mode(), "LIVE ARMED")


class RestartRecoveryTests(unittest.TestCase):
    def live_with_trailed_position(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        r.eng.analysis_pass()
        pos = next(iter(r.broker.pos.values()))
        entry = pos.price_open
        self.initial_sl = pos.sl                                   # `pos` is the broker's live object: remember where the stop began
        r.eng.state_pass()                                         # reconcile registers trailing state
        for bid in (entry + 4.0, entry + 9.0, entry + 11.0):
            r.broker.set_quote("XAUUSD", bid, bid + 0.25)
            r.eng.manage_pass()
            time.sleep(0.45)                                       # outlast the per-position modify back-off
        return r, pos, entry

    def test_trailing_stop_state_survives_a_restart_and_never_weakens(self):
        r, pos, entry = self.live_with_trailed_position()
        trailed = r.broker.pos[pos.ticket].sl
        self.assertGreater(trailed, self.initial_sl)               # it trailed
        r.eng.state_pass()
        row = r.journal.positions("live", "open")[0]
        self.assertGreaterEqual(row["high_water"], entry + 11.0)
        self.assertAlmostEqual(row["last_ack_sl"], trailed, places=2)

        # restart while price has fallen back; the VPS comes up with fresh memory
        r.broker.set_quote("XAUUSD", entry + 6.0, entry + 6.25)
        r2 = Rig(r.broker, r.journal, allow_live=True).start()
        r2.eng.state_pass()                                        # reconcile
        st = r2.eng.trailing.states[("live", pos.ticket)]
        self.assertGreaterEqual(st.high_water, entry + 11.0)       # water mark restored, not reset to the lower price
        self.assertEqual(r2.eng.compute_mode(), "PAUSED")          # and still no new entries
        r2.eng.manage_pass()
        self.assertGreaterEqual(r.broker.pos[pos.ticket].sl, trailed)

    def test_position_closed_by_the_broker_while_offline_is_completed_from_history(self):
        r, pos, entry = self.live_with_trailed_position()
        sl = r.broker.pos[pos.ticket].sl
        r.broker.close_position_at(pos.ticket, sl, DEAL_REASON_SL)  # stop hit while the engine was down
        r2 = Rig(r.broker, r.journal, allow_live=True).start()
        r2.eng.state_pass()
        self.assertEqual(r.journal.positions("live", "open"), [])
        closed = r.journal.positions("live", "closed")[0]
        self.assertEqual(closed["close_reason"], "trailing_stop")
        self.assertGreater(closed["pnl"], 0)
        self.assertNotIn(("live", pos.ticket), r2.eng.trailing.states)

    def test_unknown_bot_position_is_adopted_and_foreign_positions_are_left_alone(self):
        from scalper_engine.models import Position
        r = Rig(allow_live=True).start()
        r.broker.pos[7] = Position(7, "XAUUSD", "BUY", 0.05, 2390.0, 2385.0, 0.0, 2392.0, 0.0, 0.0, 20260117, "FAIPmystery", 1)
        r.broker.pos[8] = Position(8, "XAUUSD", "BUY", 0.05, 2390.0, 0.0, 0.0, 2392.0, 0.0, 0.0, 0, "manual trade", 1)
        r.eng.state_pass()
        self.assertEqual([p["ticket"] for p in r.journal.positions("live", "open")], [7])
        self.assertTrue(any(e["kind"] == "unexpected_position" for e in r.journal.events))
        self.assertEqual(r.broker.pos[8].sl, 0.0)                  # the user's manual trade was not modified
        self.assertIn(("live", 7), r.eng.trailing.states)
        self.assertNotIn(("live", 8), r.eng.trailing.states)

    def test_unprotected_bot_position_is_given_a_stop_on_startup(self):
        from scalper_engine.models import Position
        r = Rig(allow_live=True).start()
        r.eng.atr_cache["XAUUSD"] = 2.0
        r.broker.pos[7] = Position(7, "XAUUSD", "BUY", 0.05, 2392.0, 0.0, 0.0, 2392.0, 0.0, 0.0, 20260117, "FAIPmystery", 1)
        r.eng.state_pass()
        self.assertGreater(r.broker.pos[7].sl, 0)
        self.assertLess(r.broker.pos[7].sl, 2392.0)
        self.assertTrue(any(e["kind"] == "missing_stop" for e in r.journal.events))

    def test_pending_order_intent_left_by_a_crash_is_resolved_from_broker_state_not_resent(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        r.broker.open_script = ["timeout_filled"]
        r.eng.analysis_pass()                                      # resolves in-process, but simulate a crash before that:
        sig = list(r.journal.signals.values())[0]["signal_id"]
        r.journal.executions[(sig, "live")]["state"] = "SENT"      # the process "died" with the row unresolved
        sent_before = len([x for x in r.broker.sent if x["action"] == 1 and "position" not in x])
        r2 = Rig(r.broker, r.journal, allow_live=True).start()
        r2.eng.state_pass()
        self.assertEqual(r.journal.executions[(sig, "live")]["state"], "FILLED")
        self.assertEqual(len([x for x in r.broker.sent if x["action"] == 1 and "position" not in x]), sent_before)


class ControlTests(unittest.TestCase):
    def test_emergency_stop_blocks_entries_and_disarms_but_keeps_managing_open_trades(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        r.eng.analysis_pass()
        self.assertEqual(len(r.broker.pos), 1)
        self.assertEqual(r.cmd("emergency_stop")[0], "done")
        self.assertTrue(r.eng.paused)
        self.assertFalse(r.eng.live_armed)
        self.assertEqual(r.eng.exec_mode, "none")
        self.assertEqual(len(r.broker.pos), 1)                     # NOT silently closed
        self.assertGreater(next(iter(r.broker.pos.values())).sl, 0)
        r.new_bar()
        r.eng.analysis_pass()
        self.assertEqual(len([x for x in r.broker.sent if x["action"] == 1 and "position" not in x]), 1)
        self.assertEqual(r.cmd("resume_entries")[0], "rejected")   # must re-arm deliberately

    def test_closing_positions_is_a_separate_explicit_command_and_verified(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        r.eng.analysis_pass()
        r.cmd("emergency_stop")
        self.assertEqual(len(r.broker.pos), 1)
        st, res = r.cmd("close_positions", mode="live")
        self.assertEqual(st, "done")
        self.assertEqual(len(res["closed"]), 1)
        self.assertEqual(r.broker.pos, {})
        self.assertEqual(r.cmd("close_positions", mode="bogus")[0], "rejected")

    def test_close_failure_is_reported_as_failed_with_a_critical_alert(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        r.eng.analysis_pass()
        r.broker.close_fail = 100
        st, res = r.cmd("close_positions", mode="live")
        self.assertEqual(st, "failed")
        self.assertEqual(len(res["failed"]), 1)
        self.assertTrue(any(e["kind"] == "close_failed" and e["severity"] == "critical" for e in r.journal.events))

    def test_ambiguous_order_locks_trading_until_a_person_unlocks_it(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        r.broker.open_script = ["timeout_lost"]
        r.eng.analysis_pass()
        self.assertTrue(r.eng.locked)
        self.assertEqual(r.eng.compute_mode(), "ERROR")
        self.assertEqual(r.cmd("resume_entries")[0], "rejected")
        r.eng.state_pass()
        st, res = r.cmd("request_live_preview")
        self.assertEqual(st, "rejected")                           # cannot re-arm while locked / criticals open
        st, res = r.cmd("unlock_trading")
        self.assertEqual(st, "done")
        self.assertFalse(r.eng.locked)
        self.assertTrue(r.eng.paused)                              # unlocking never resumes trading by itself

    def test_unlock_refuses_while_a_position_has_no_stop(self):
        from scalper_engine.models import Position
        r = Rig(allow_live=True).start()
        r.eng.trip_lock("test")
        r.broker.pos[9] = Position(9, "XAUUSD", "BUY", 0.05, 2390.0, 0.0, 0.0, 2392.0, 0.0, 0.0, 20260117, "FAIPx", 1)
        self.assertEqual(r.cmd("unlock_trading")[0], "rejected")
        self.assertTrue(r.eng.locked)

    def test_daily_loss_limit_locks_new_entries_and_keeps_positions_under_pause_policy(self):
        r = Rig().start()
        r.cmd("start_paper")
        r.eng.analysis_pass()
        self.assertEqual(len(r.eng.paper_gw.positions()), 1)
        r.journal.upsert_position({"mode": "paper", "ticket": 1, "status": "closed", "pnl": -250.0, "closed_at": now_iso(), "opened_at": now_iso()})
        r.eng._risk_cache("paper", force=True)
        r.eng.state_pass()
        self.assertTrue(r.eng.locked)
        self.assertIn("daily loss", r.eng.lock_reason)
        self.assertEqual(len(r.eng.paper_gw.positions()), 1)       # policy 'pause': existing trade kept and managed
        self.assertTrue(r.eng.paused)
        self.assertEqual(r.eng.compute_mode(), "ERROR")

    def test_daily_loss_limit_with_close_all_policy_flattens(self):
        j = MemoryJournal()
        j.config = {**STORED_CONFIG, "risk": {"breach_policy": "close_all"}}
        r = Rig(journal=j).start()
        r.cmd("start_paper")
        r.eng.analysis_pass()
        self.assertEqual(len(r.eng.paper_gw.positions()), 1)
        j.upsert_position({"mode": "paper", "ticket": 1, "status": "closed", "pnl": -250.0, "closed_at": now_iso(), "opened_at": now_iso()})
        r.eng._risk_cache("paper", force=True)
        r.eng.state_pass()
        self.assertTrue(r.eng.locked)
        self.assertEqual(r.eng.paper_gw.positions(), [])

    def test_consecutive_losses_lock_trading(self):
        r = Rig().start()
        r.cmd("start_paper")
        for i in range(3):
            r.journal.upsert_position({"mode": "paper", "ticket": 10 + i, "status": "closed", "pnl": -5.0, "closed_at": now_iso(), "opened_at": now_iso()})
        r.eng._risk_cache("paper", force=True)
        r.eng.state_pass()
        self.assertTrue(r.eng.locked)
        self.assertIn("consecutive", r.eng.lock_reason)


class ConnectionLossTests(unittest.TestCase):
    def test_disconnect_is_reported_then_reconnect_reconciles(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        r.eng.analysis_pass()
        ticket = next(iter(r.broker.pos))
        r.broker.connected = False
        r.eng.state_pass()
        self.assertFalse(r.eng.connected)
        self.assertEqual(r.eng.compute_mode(), "DISCONNECTED")
        crit = [e for e in r.journal.events if e["kind"] == "mt5_disconnected"]
        self.assertTrue(crit and "broker stops" in crit[0]["message"])    # honest about what still protects the trade
        r.broker.connected = True
        r.eng.last_reconnect_try = 0
        r.eng.state_pass()
        self.assertTrue(r.eng.connected)
        self.assertIn(("live", ticket), r.eng.trailing.states)
        self.assertTrue(any(e["kind"] == "mt5_reconnected" for e in r.journal.events))

    def test_unreadable_positions_never_look_like_flat(self):
        r = Rig(allow_live=True).start()
        r.go_live()
        r.eng.analysis_pass()
        r.broker.fail_reads = True
        r.eng.manage_pass()
        self.assertFalse(r.eng.health.ok("positions_live"))
        self.assertEqual(len(r.broker.pos), 1)
        r.broker.fail_reads = False
        r.eng.state_pass()
        self.assertEqual(len(r.journal.positions("live", "open")), 1)   # not wrongly marked closed

    def test_database_outage_stops_new_entries_but_not_position_management(self):
        r = Rig().start()
        r.cmd("start_paper")
        r.journal.fail_writes = True
        r.eng.analysis_pass()                                      # cannot store the signal -> must not trade
        self.assertEqual(r.eng.paper_gw.positions(), [])
        r.eng.state_pass()                                         # must not raise
        self.assertFalse(r.eng.health.ok("database"))


class OpsApiTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig().start()
        self.srv = make_server(self.r.eng, "127.0.0.1", 0, "tok-abcdef-123456")
        import threading
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path, token=None):
        rq = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(rq, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_reflects_connection(self):
        self.assertEqual(self.get("/health")[0], 200)
        self.r.eng.connected = False
        self.assertEqual(self.get("/health")[0], 503)

    def test_status_requires_the_token_and_leaks_no_secrets(self):
        self.assertEqual(self.get("/status")[0], 401)
        self.assertEqual(self.get("/status", "wrong")[0], 401)
        st, body = self.get("/status", "tok-abcdef-123456")
        self.assertEqual(st, 200)
        blob = json.dumps(body)
        for secret in ("svc-key-123456", "tok-abcdef-123456"):
            self.assertNotIn(secret, blob)
        self.assertEqual(body["mode"], "PAUSED")

    def test_no_trading_endpoints_exist(self):
        for path in ("/order", "/trade", "/close", "/arm"):
            self.assertEqual(self.get(path)[0], 404)

    def test_openapi_document_is_served(self):
        st, body = self.get("/openapi.json")
        self.assertEqual(st, 200)
        self.assertIn("/health", body["paths"])


if __name__ == "__main__":
    unittest.main()

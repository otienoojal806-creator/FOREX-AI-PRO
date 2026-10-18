import io
import json
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from scalper_engine.config import EnvSettings, default_config, effective_config
from scalper_engine.monitoring_and_alerts import Alerts, setup_logging
from scalper_engine.trade_journal import JournalError, MemoryJournal, SupabaseJournal

KEY = "service-role-key-SECRET-123456"


class Stub:
    """A tiny PostgREST stand-in that records requests and replays scripted responses."""

    def __init__(self):
        self.requests, self.script = [], []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): ...

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                stub.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
                code, payload = stub.script.pop(0) if stub.script else (200, [])
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = _do

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class SupabaseJournalTests(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.j = SupabaseJournal(self.stub.url, KEY, "user-1", timeout=3)
        patcher = mock.patch("scalper_engine.trade_journal.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.stub.close)

    def last(self):
        return self.stub.requests[-1]

    def test_requests_carry_the_service_key_and_scope_every_query_to_the_user(self):
        self.j.get_execution("sig1", "live")
        r = self.last()
        hdr = {k.lower(): v for k, v in r["headers"].items()}          # header names are case-insensitive
        self.assertEqual(hdr["apikey"], KEY)
        self.assertEqual(hdr["authorization"], f"Bearer {KEY}")
        self.assertIn("user_id=eq.user-1", r["path"])
        self.assertIn("signal_id=eq.sig1", r["path"])

    def test_writes_inject_the_user_id_and_use_the_unique_keys_for_upserts(self):
        self.j.upsert_execution({"signal_id": "s", "mode": "paper", "state": "INTENT"})
        r = self.last()
        self.assertEqual(r["body"]["user_id"], "user-1")
        self.assertIn("on_conflict=user_id%2Csignal_id%2Cmode", r["path"].replace(",", "%2C"))
        self.assertIn("merge-duplicates", {k.lower(): v for k, v in r["headers"].items()}["prefer"])
        self.j.upsert_position({"mode": "live", "ticket": 5, "status": "open"})
        self.assertIn("on_conflict=user_id%2Cmode%2Cticket", self.last()["path"].replace(",", "%2C"))

    def test_duplicate_signal_insert_reports_false(self):
        self.stub.script = [(201, [{"signal_id": "s"}]), (201, [])]
        self.assertTrue(self.j.insert_signal({"signal_id": "s"}))
        self.assertFalse(self.j.insert_signal({"signal_id": "s"}))     # PostgREST returns [] for an ignored duplicate
        self.assertIn("ignore-duplicates", {k.lower(): v for k, v in self.last()["headers"].items()}["prefer"])

    def test_server_errors_are_retried_but_client_errors_are_not(self):
        self.stub.script = [(503, {}), (200, [{"config": {"a": 1}}])]
        self.assertEqual(self.j.get_config(), {"a": 1})
        self.assertEqual(len(self.stub.requests), 2)
        self.stub.requests.clear()
        self.stub.script = [(400, {"message": "bad"})]
        with self.assertRaises(JournalError):
            self.j.get_config()
        self.assertEqual(len(self.stub.requests), 1)

    def test_persistent_failure_raises_and_the_error_never_contains_the_key(self):
        self.stub.script = [(500, {})] * 5
        with self.assertRaises(JournalError) as cm:
            self.j.get_state()
        self.assertNotIn(KEY, str(cm.exception))
        self.assertEqual(len(self.stub.requests), 3)                    # 1 try + 2 retries: bounded

    def test_unreachable_database_raises_a_clean_error(self):
        j = SupabaseJournal("http://127.0.0.1:9", KEY, "user-1", timeout=1)
        with self.assertRaises(JournalError) as cm:
            j.get_state()
        self.assertNotIn(KEY, str(cm.exception))

    def test_claiming_commands_expires_old_ones_and_claims_fresh_ones_atomically(self):
        import datetime as dt
        fresh = dt.datetime.now(dt.timezone.utc).isoformat()
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)).isoformat()
        self.stub.script = [
            (200, [{"id": "old", "command": "arm_live", "created_at": old}, {"id": "new", "command": "pause_entries", "created_at": fresh}]),
            (200, []),                                                  # PATCH finishing the expired command
            (200, [{"id": "new", "command": "pause_entries", "status": "processing"}]),   # atomic claim
        ]
        got = self.j.claim_commands(90)
        self.assertEqual([c["id"] for c in got], ["new"])
        patches = [r for r in self.stub.requests if r["method"] == "PATCH"]
        self.assertEqual(patches[0]["body"]["status"], "rejected")
        self.assertIn("status=eq.pending", patches[1]["path"])           # claim only succeeds if still pending

    def test_startup_discards_commands_in_flight(self):
        self.stub.script = [(200, [{"id": "a"}, {"id": "b"}])]
        self.assertEqual(self.j.expire_pending_commands(), 2)
        self.assertIn("in.(pending,processing)", self.last()["path"].replace("%2C", ","))

    def test_audit_goes_through_the_append_only_rpc(self):
        self.j.audit("user", "live.armed", {"login": 1})
        r = self.last()
        self.assertIn("rpc/scalper_audit_append", r["path"])
        self.assertEqual(r["body"]["_action"], "live.armed")

    def test_closed_since_encodes_the_timestamp_offset(self):
        self.j.closed_since("live", "2026-10-17T00:00:00+00:00")
        path = self.last()["path"]
        self.assertIn("closed_at=gte.2026-10-17T00:00:00%2B00:00", path)
        self.assertNotIn("+00:00", path)                                  # a raw '+' would be read as a space


class ConfigTests(unittest.TestCase):
    def test_dashboard_settings_are_clamped_to_the_vps_hard_caps(self):
        env = EnvSettings(hard_max_risk_pct=0.5, hard_max_daily_loss_pct=3.0, hard_max_positions=2)
        cfg, warns = effective_config({"risk": {"risk_per_trade_pct": 2.0, "max_daily_loss_pct": 10, "max_total_positions": 9,
                                                "max_aggregate_open_risk_pct": 8}}, env)
        self.assertEqual(cfg["risk"]["risk_per_trade_pct"], 0.5)
        self.assertEqual(cfg["risk"]["max_daily_loss_pct"], 3.0)
        self.assertEqual(cfg["risk"]["max_total_positions"], 2)
        self.assertLessEqual(cfg["risk"]["max_aggregate_open_risk_pct"], 3.0)
        self.assertTrue(len(warns) >= 3)

    def test_defaults_follow_the_spec_and_deep_merge_keeps_unlisted_values(self):
        cfg, warns = effective_config({"instruments": {"XAUUSD": {"max_lots": 0.2}}}, EnvSettings())
        self.assertEqual(cfg["risk"]["risk_per_trade_pct"], 0.25)       # spec default
        self.assertEqual(cfg["instruments"]["XAUUSD"]["max_lots"], 0.2)
        self.assertEqual(cfg["instruments"]["XAUUSD"]["broker_symbol"], "XAUUSD")
        self.assertEqual(cfg["risk"]["breach_policy"], "pause")

    def test_unknown_breach_policy_falls_back_to_pause_and_empty_symbol_disables_the_instrument(self):
        cfg, warns = effective_config({"risk": {"breach_policy": "yolo"}, "instruments": {"BTCUSD": {"broker_symbol": " "}}}, EnvSettings())
        self.assertEqual(cfg["risk"]["breach_policy"], "pause")
        self.assertFalse(cfg["instruments"]["BTCUSD"]["enabled"])

    def test_instruments_have_independent_trailing_and_strategy_settings(self):
        d = default_config()["instruments"]
        self.assertNotEqual(d["XAUUSD"]["trail"], d["BTCUSD"]["trail"])
        self.assertNotEqual(d["XAUUSD"]["max_spread_points"], d["BTCUSD"]["max_spread_points"])

    def test_env_parsing_and_validation(self):
        e = EnvSettings.from_env({"SUPABASE_URL": "https://a.supabase.co/", "SUPABASE_SERVICE_ROLE_KEY": "k", "ENGINE_USER_ID": "x" * 36,
                                  "MT5_LOGIN": "123", "ENGINE_ALLOW_LIVE": "true"})
        self.assertEqual(e.supabase_url, "https://a.supabase.co")
        self.assertTrue(e.allow_live)
        self.assertFalse(e.allow_real_account)                           # off unless explicitly enabled
        self.assertEqual(e.validate(), [])
        self.assertTrue(EnvSettings().validate())                        # empty config is rejected
        self.assertFalse(EnvSettings.from_env({}).allow_live)            # live is OFF by default

    def test_secrets_are_not_shown_in_repr(self):
        e = EnvSettings(service_key="AAAA-secret", mt5_password="BBBB-secret", api_token="CCCC-secret", telegram_token="DDDD-secret")
        for s in ("AAAA-secret", "BBBB-secret", "CCCC-secret", "DDDD-secret"):
            self.assertNotIn(s, repr(e))


class LoggingTests(unittest.TestCase):
    def test_secrets_are_redacted_from_log_output_even_when_embedded_in_messages_and_args(self):
        buf = io.StringIO()
        setup_logging("INFO", ["hunter2-PASSWORD", "tok-abcdef-123456"])
        root = logging.getLogger()
        root.handlers[0].stream = buf
        logging.getLogger("t").info("login with hunter2-PASSWORD failed")
        logging.getLogger("t").warning("header Bearer %s sent", "tok-abcdef-123456")
        try:
            raise RuntimeError("boom hunter2-PASSWORD")
        except RuntimeError:
            logging.getLogger("t").exception("crash")
        out = buf.getvalue()
        self.assertNotIn("hunter2-PASSWORD", out)
        self.assertNotIn("tok-abcdef-123456", out)
        self.assertIn("***", out)
        for line in out.strip().splitlines():
            json.loads(line)                                             # structured: every line is valid JSON
        root.handlers.clear()

    def test_repeated_alerts_are_deduplicated_but_distinct_ones_are_not(self):
        j = MemoryJournal()
        t = [1000.0]
        a = Alerts(j, clock=lambda: t[0], dedupe_sec=60)
        for _ in range(5):
            a.emit("critical", "x", "same message")
        a.emit("critical", "y", "other message")
        self.assertEqual(len(j.events), 2)
        t[0] += 61
        a.emit("critical", "x", "same message")
        self.assertEqual(len(j.events), 3)

    def test_alert_storage_failure_never_raises(self):
        j = MemoryJournal()
        j.add_risk_event = mock.Mock(side_effect=RuntimeError("db down"))
        Alerts(j, dedupe_sec=0).emit("critical", "x", "m")               # must not propagate into the trading loop


if __name__ == "__main__":
    unittest.main()

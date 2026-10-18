import threading
import unittest

from scalper_engine.remote_control import DeadMansSwitch, TelegramControl

from .test_engine import Rig

CHAT = "424242"


def upd(uid, text, chat=CHAT, sender=CHAT, ctype="private", bot=False):
    return {"update_id": uid, "message": {"text": text, "chat": {"id": int(chat), "type": ctype}, "from": {"id": int(sender), "is_bot": bot}}}


class FakeTelegram:
    def __init__(self, batches):
        self.batches, self.sent, self.calls = list(batches), [], []

    def __call__(self, url, payload, timeout):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, payload))
        if method == "getUpdates":
            return {"ok": True, "result": self.batches.pop(0) if self.batches else []}
        if method == "sendMessage":
            self.sent.append(payload["text"])
        return {"ok": True}


class TelegramControlTests(unittest.TestCase):
    def setUp(self):
        self.rig = Rig().start()
        self.eng = self.rig.eng

    def make(self, *batches):
        tg = FakeTelegram(batches)
        return TelegramControl(self.eng, "bot-token-123456", CHAT, http=tg, long_poll_sec=1), tg

    def test_backlog_from_downtime_is_discarded_on_first_poll(self):
        self.eng.paused = False
        ctl, tg = self.make([upd(1, "/stop")], [])
        ctl.poll_once()
        self.assertEqual(tg.sent, [])
        self.assertFalse(self.eng.paused)  # the old /stop must NOT fire after a restart... and did not change state
        self.assertEqual(ctl._offset, 2)

    def test_pause_and_stop_work_and_are_audited(self):
        self.eng.paused, self.eng.exec_mode = False, "paper"
        ctl, tg = self.make([], [upd(2, "/pause"), upd(3, "/stop")])
        ctl.poll_once(); ctl.poll_once()
        self.assertTrue(self.eng.paused)
        self.assertEqual(self.eng.exec_mode, "none")
        self.assertEqual(len(tg.sent), 2)
        actors = [(a["actor"], a["action"]) for a in self.rig.journal.audits]
        self.assertIn(("telegram", "telegram.pause"), actors)
        self.assertIn(("telegram", "telegram.emergency_stop"), actors)

    def test_strangers_groups_and_bots_are_ignored(self):
        self.eng.paused, self.eng.exec_mode = False, "paper"
        ctl, tg = self.make([], [upd(2, "/stop", chat="999", sender="999"), upd(3, "/stop", ctype="group"),
                                 upd(4, "/stop", sender="999"), upd(5, "/stop", bot=True)])
        ctl.poll_once(); ctl.poll_once()
        self.assertFalse(self.eng.paused)
        self.assertEqual(tg.sent, [])

    def test_dangerous_commands_do_not_exist_over_telegram(self):
        self.eng.paused, self.eng.exec_mode, self.eng.locked = True, "none", True
        ctl, _ = self.make()
        for c in ("/resume", "/arm", "/unlock", "/close", "/closeall", "/live"):
            self.assertTrue(ctl.handle(c).startswith("Unknown command"), c)
        self.assertTrue(self.eng.paused)
        self.assertTrue(self.eng.locked)

    def test_status_positions_today_render(self):
        ctl, _ = self.make()
        self.assertIn("Mode:", ctl.handle("/status"))
        self.assertEqual(ctl.handle("/positions@MyBot"), "No open trades.")
        self.assertIn("LIVE:", ctl.handle("/today"))
        self.assertIn("/stop", ctl.handle("/help"))


class DeadMansSwitchTests(unittest.TestCase):
    def test_pings_only_when_healthy_and_throttled(self):
        rig = Rig().start()
        eng = rig.eng
        t = [1000.0]
        pings = []
        dm = DeadMansSwitch(eng, "https://hc.example/ping/abc", http=lambda u, p, to: pings.append(u), period_sec=60, clock=lambda: t[0])
        self.assertFalse(dm.tick())  # loops not started -> not healthy
        ev = threading.Event()
        eng.threads = [threading.Thread(target=ev.wait, daemon=True)]
        eng.threads[0].start()
        eng.health.set("database", True, "")
        self.assertTrue(dm.tick())
        self.assertFalse(dm.tick())  # throttled
        t[0] += 61
        eng.connected = False
        self.assertFalse(dm.tick())  # MT5 down -> silence, so the external service alerts you
        eng.connected = True
        eng.health.set("database", False, "x")
        self.assertFalse(dm.tick())
        eng.health.set("database", True, "")
        self.assertTrue(dm.tick())
        self.assertEqual(len(pings), 2)
        ev.set()

    def test_ping_url_is_redacted_from_logs(self):
        from scalper_engine.config import EnvSettings
        self.assertIn("https://hc.example/ping/abc", EnvSettings(ping_url="https://hc.example/ping/abc").secrets())


if __name__ == "__main__":
    unittest.main()

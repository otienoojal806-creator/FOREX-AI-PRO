"""Phone remote control and dead-man's switch.

Why this exists: the web app only helps while it is open on your screen. Two things must work with a LOCKED phone:

1. Telegram control. Send /status, /positions, /today, /pause or /stop to your own bot and get an answer in seconds.
   Deliberately limited to SAFE actions. It can only make the bot do LESS (pause entries, emergency stop). It can never
   resume, arm live, unlock, change settings or close trades. Those need the app and their typed confirmations.
2. Dead-man's switch. While the engine is healthy it pings an external URL (for example a free healthchecks.io check)
   every minute. If the VPS, MT5 or the engine hangs or dies, the pings stop and that service alerts your phone. The
   engine cannot report its own death, so this is the only way to hear about it.

Security: only messages from the configured TELEGRAM_CHAT_ID (a private chat with the same numeric user id) are acted
on. Everything else is ignored silently. Every action is written to the audit log with actor "telegram".
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger("remote")

HELP = (
    "ForexAI Scalper remote\n"
    "/status  mode, MT5, account, open trades\n"
    "/positions  open trades with stops\n"
    "/today  today's result\n"
    "/pause  pause NEW entries (trades stay managed)\n"
    "/stop  emergency stop: disable entries and disarm live (trades stay protected and managed)\n\n"
    "Resuming, arming live, unlocking and closing trades are only possible in the app."
)

HttpJson = Callable[[str, Optional[dict], float], Any]


def _http_json(url: str, payload: Optional[dict], timeout: float) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    rq = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    with urllib.request.urlopen(rq, timeout=timeout) as r:  # noqa: S310 (fixed https endpoints from config)
        raw = r.read().decode() or "{}"
    try:
        return json.loads(raw)
    except ValueError:  # ping services answer plain text such as "OK"
        return {}


def _money(v: float) -> str:
    return f"{v:+,.2f}"


class TelegramControl:
    def __init__(self, engine: "Engine", token: str, chat_id: str, http: HttpJson = _http_json, long_poll_sec: int = 25):
        self.engine, self._tok, self._chat, self._http, self._poll = engine, token, str(chat_id).strip(), http, long_poll_sec
        self._offset: Optional[int] = None
        self.handled = 0

    # ------------------------------------------------------------------ plumbing
    def _api(self, method: str, payload: dict, timeout: float = 10) -> Any:
        return self._http(f"https://api.telegram.org/bot{self._tok}/{method}", payload, timeout)

    def _send(self, text: str) -> None:
        try:
            self._api("sendMessage", {"chat_id": self._chat, "text": text[:3500]})
        except Exception as exc:
            log.warning("telegram reply failed (%s)", type(exc).__name__)

    def authorised(self, msg: dict) -> bool:
        chat, sender = msg.get("chat") or {}, msg.get("from") or {}
        return (chat.get("type") == "private" and str(chat.get("id")) == self._chat and str(sender.get("id")) == self._chat
                and not sender.get("is_bot", False))

    # ------------------------------------------------------------------ commands
    def handle(self, text: str) -> str:
        cmd = (text or "").strip().split()[0].lower().split("@")[0] if (text or "").strip() else ""
        e = self.engine
        if cmd in ("/start", "/help"):
            return HELP
        if cmd == "/status":
            return self.status()
        if cmd == "/positions":
            return self.positions()
        if cmd == "/today":
            return self.today()
        if cmd == "/pause":
            st, res = e.handle_command("pause_entries", {})
            e._safe_audit("telegram", "telegram.pause", {"status": st})
            return f"Entries paused ({st}). Open trades keep being managed." if st == "done" else f"Pause {st}: {res}"
        if cmd == "/stop":
            st, res = e.handle_command("emergency_stop", {})
            e._safe_audit("telegram", "telegram.emergency_stop", {"status": st})
            return ("EMERGENCY STOP done. New entries disabled, live disarmed. Open trades still have their stops and are still "
                    "managed. Close them from the app if you want them closed." if st == "done" else f"Stop {st}: {res}")
        return "Unknown command.\n\n" + HELP

    def status(self) -> str:
        e = self.engine
        lines = [f"Mode: {e.compute_mode()}", f"MT5: {'connected' if e.connected else 'DISCONNECTED'}"]
        if e.locked:
            lines.append(f"LOCKED: {e.lock_reason}")
        try:
            a = e.broker.account_info() if e.connected else None
        except Exception:
            a = None
        if a:
            lines.append(f"Account {a.login} ({a.type_name}) equity {a.equity:,.2f} {a.currency}")
        lines.append(f"Open trades: {len(e.live_pos)} live, {len(e.paper_pos)} paper")
        for name, q in (e.quotes or {}).items():
            lines.append(f"{name} {q.get('bid')} / {q.get('ask')} spread {q.get('spread_points')}")
        return "\n".join(lines)

    def positions(self) -> str:
        e = self.engine
        rows = [("LIVE", p) for p in e.live_pos] + [("PAPER", p) for p in e.paper_pos]
        if not rows:
            return "No open trades."
        out = []
        for mode, p in rows:
            stop = f"SL {p.sl}" if p.sl else "NO STOP LOSS!"
            out.append(f"{mode} {e.inst_of.get(p.symbol, p.symbol)} {p.side} {p.volume} @ {p.price_open} · {stop} · P&L {_money(p.profit + p.swap)}")
        return "\n".join(out)

    def today(self) -> str:
        e = self.engine
        out = []
        for m in ("live", "paper"):
            rc = e.risk_cache.get(m) or {}
            floating = sum(p.profit + p.swap for p in (e.live_pos if m == "live" else e.paper_pos))
            out.append(f"{m.upper()}: realized {_money(float(rc.get('realized', 0.0)))} · floating {_money(floating)} · "
                       f"loss streak {rc.get('consecutive', 0)}")
        return "\n".join(out)

    # ------------------------------------------------------------------ polling
    def poll_once(self) -> None:
        payload: dict[str, Any] = {"timeout": 0 if self._offset is None else self._poll, "allowed_updates": ["message"]}
        if self._offset is not None:
            payload["offset"] = self._offset
        res = self._api("getUpdates", payload, timeout=self._poll + 10)
        updates = res.get("result", []) if isinstance(res, dict) else []
        first = self._offset is None
        for u in updates:
            self._offset = int(u["update_id"]) + 1
            if first:  # discard anything sent while the engine was down: stale commands must never fire on restart
                continue
            msg = u.get("message") or {}
            if not self.authorised(msg):
                continue
            self.handled += 1
            try:
                reply = self.handle(msg.get("text", ""))
            except Exception as exc:
                log.exception("telegram command crashed")
                reply = f"Command failed: {type(exc).__name__}"
            self._send(reply)
        if self._offset is None:
            self._offset = 0

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                log.warning("telegram poll failed (%s)", type(exc).__name__)
                stop.wait(5.0)


class DeadMansSwitch:
    """Pings an external URL while, and only while, the engine is genuinely healthy."""

    def __init__(self, engine: "Engine", url: str, http: HttpJson = _http_json, period_sec: float = 60.0, clock: Callable[[], float] = time.time):
        self.engine, self.url, self._http, self.period, self._clock = engine, url, http, period_sec, clock
        self.last_ping = 0.0

    def healthy(self) -> bool:
        e = self.engine
        loops_alive = bool(e.threads) and all(t.is_alive() for t in e.threads)
        return loops_alive and e.connected and e.health.ok("database") and not e.stop_event.is_set()

    def tick(self) -> bool:
        if not self.url or not self.healthy() or self._clock() - self.last_ping < self.period:
            return False
        try:
            self._http(self.url, None, 8)
            self.last_ping = self._clock()
            return True
        except Exception as exc:
            log.warning("dead-man ping failed (%s)", type(exc).__name__)
            return False

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(5.0)

"""Structured logging with secret redaction, operator alerts (dashboard + optional Telegram) and health tracking."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.request
from typing import Any, Callable, Optional

from .trade_journal import Journal


class _Redactor(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self._secrets:
            msg = msg.replace(s, "***")
        record.msg, record.args = msg, None
        return True


class _JsonFormatter(logging.Formatter):
    """One JSON object per line. Secrets are scrubbed from the FINAL text, so they cannot leak through the
    message, its arguments, or an exception line."""

    def __init__(self, secrets: Optional[list[str]] = None):
        super().__init__()
        self._secrets = [s for s in (secrets or []) if len(s) >= 6]

    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info).splitlines()[-1]  # one line, no payloads
        out = json.dumps(d)
        for s in self._secrets:
            out = out.replace(s, "***").replace(json.dumps(s)[1:-1], "***")  # also the JSON-escaped form
        return out


def setup_logging(level: str, secrets: list[str], log_file: Optional[str] = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level, logging.INFO))
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5))
    for h in handlers:
        h.setFormatter(_JsonFormatter(secrets))
        h.addFilter(_Redactor(secrets))
        root.addHandler(h)


class Alerts:
    """Writes risk events to the database and pushes warnings/criticals to the operator's phone via Telegram."""

    def __init__(self, journal: Journal, telegram_token: str = "", telegram_chat_id: str = "",
                 clock: Callable[[], float] = time.time, dedupe_sec: float = 60.0):
        self.journal, self._tok, self._chat, self._clock, self._dedupe = journal, telegram_token, telegram_chat_id, clock, dedupe_sec
        self._seen: dict[str, float] = {}
        self.log = logging.getLogger("alert")
        self.recent: list[dict] = []

    def emit(self, severity: str, kind: str, message: str, details: Optional[dict] = None) -> None:
        now = self._clock()
        key = f"{severity}:{kind}:{message}"
        if now - self._seen.get(key, 0) < self._dedupe:
            return
        self._seen[key] = now
        details = details or {}
        self.recent = (self.recent + [{"t": now, "severity": severity, "kind": kind, "message": message}])[-50:]
        getattr(self.log, "error" if severity == "critical" else "warning" if severity == "warn" else "info")("%s: %s", kind, message)
        try:
            self.journal.add_risk_event(severity, kind, message, details)
        except Exception as exc:
            self.log.error("could not store risk event (%s)", exc)
        if severity in ("critical", "warn") and self._tok and self._chat:
            icon = "🚨" if severity == "critical" else "⚠️"
            threading.Thread(target=self._telegram, args=(f"{icon} ForexAI Scalper: {message}",), daemon=True).start()

    def _telegram(self, text: str) -> None:
        try:
            body = json.dumps({"chat_id": self._chat, "text": text[:900]}).encode()
            rq = urllib.request.Request(f"https://api.telegram.org/bot{self._tok}/sendMessage", data=body,
                                        headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(rq, timeout=6).read()
        except Exception as exc:
            self.log.warning("telegram alert failed (%s)", type(exc).__name__)


class Health:
    """Named health checks used by the dashboard, the health endpoint and the live-arming gate."""

    def __init__(self) -> None:
        self.checks: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self._lock = threading.Lock()

    def set(self, name: str, ok: bool, detail: str = "") -> None:
        with self._lock:
            self.checks[name] = {"ok": bool(ok), "detail": detail, "at": time.time()}

    def ok(self, *names: str) -> bool:
        with self._lock:
            return all(self.checks.get(n, {}).get("ok") for n in names)

    def failing(self) -> list[str]:
        with self._lock:
            return [n for n, c in self.checks.items() if not c["ok"]]

    def failing_prefix(self, prefix: str) -> list[str]:
        with self._lock:
            return [n for n, c in self.checks.items() if n.startswith(prefix) and not c["ok"]]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"checks": {k: dict(v) for k, v in self.checks.items()}, "warnings": list(self.warnings)}

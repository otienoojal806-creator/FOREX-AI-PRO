"""Durable storage: signals, executions, positions, risk events, commands and the audit trail.

`SupabaseJournal` talks to the project's PostgreSQL through PostgREST using the service-role key (held only by
the engine, outbound HTTPS only). `MemoryJournal` is an in-process twin used by tests and by dry runs.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("journal")


class JournalError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Journal(ABC):
    @abstractmethod
    def get_config(self) -> Optional[dict]: ...
    @abstractmethod
    def save_config(self, cfg: dict) -> None: ...
    @abstractmethod
    def claim_commands(self, ttl_sec: float) -> list[dict]: ...
    @abstractmethod
    def finish_command(self, cid: str, status: str, result: dict) -> None: ...
    @abstractmethod
    def expire_pending_commands(self) -> int: ...
    @abstractmethod
    def upsert_state(self, row: dict) -> None: ...
    @abstractmethod
    def get_state(self) -> Optional[dict]: ...
    @abstractmethod
    def insert_signal(self, row: dict) -> bool:
        """Insert a signal. Returns False when this signal_id already exists (duplicate candle/entry)."""
    @abstractmethod
    def update_signal(self, signal_id: str, outcome: str, reject_reason: Optional[str] = None) -> None: ...
    @abstractmethod
    def upsert_execution(self, row: dict) -> None: ...
    @abstractmethod
    def get_execution(self, signal_id: str, mode: str) -> Optional[dict]: ...
    @abstractmethod
    def unresolved_executions(self, mode: str) -> list[dict]: ...
    @abstractmethod
    def upsert_position(self, row: dict) -> None: ...
    @abstractmethod
    def positions(self, mode: str, status: str) -> list[dict]: ...
    @abstractmethod
    def closed_since(self, mode: str, since_iso: str) -> list[dict]: ...
    @abstractmethod
    def add_risk_event(self, severity: str, kind: str, message: str, details: dict) -> None: ...
    @abstractmethod
    def unresolved_critical(self) -> list[dict]: ...
    @abstractmethod
    def resolve_risk_events(self) -> None: ...
    @abstractmethod
    def audit(self, actor: str, action: str, details: dict) -> None: ...

    # non-critical writes run off the hot path so a slow database never delays a stop update
    def defer(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:
            log.warning("deferred write failed: %s", exc)


class BackgroundWriter:
    def __init__(self) -> None:
        self.q: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=2000)
        self._t = threading.Thread(target=self._run, name="journal-writer", daemon=True)
        self._t.start()

    def submit(self, fn: Callable[[], None]) -> None:
        try:
            self.q.put_nowait(fn)
        except queue.Full:
            log.error("journal write queue full; dropping a non-critical write")

    def _run(self) -> None:
        while True:
            fn = self.q.get()
            for attempt in range(3):
                try:
                    fn()
                    break
                except Exception as exc:
                    log.warning("background write failed (%s), attempt %d", exc, attempt + 1)
                    time.sleep(1 + attempt)


class SupabaseJournal(Journal):
    def __init__(self, url: str, service_key: str, user_id: str, timeout: float = 8.0):
        self.base, self.key, self.uid, self.timeout = url.rstrip("/"), service_key, user_id, timeout
        self._writer = BackgroundWriter()

    def defer(self, fn: Callable[[], None]) -> None:
        self._writer.submit(fn)

    # ---- HTTP ----
    def _req(self, method: str, path: str, params: Optional[dict] = None, body: Any = None, prefer: str = "return=minimal",
             retries: int = 2) -> Any:
        q = ("?" + urllib.parse.urlencode(params, safe=",.:()*")) if params else ""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "Prefer": prefer}
        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                rq = urllib.request.Request(f"{self.base}/rest/v1/{path}{q}", data=data, headers=headers, method=method)
                with urllib.request.urlopen(rq, timeout=self.timeout) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:300]
                if e.code < 500 and e.code != 429:
                    raise JournalError(f"{method} {path} -> {e.code}: {msg}") from None
                last = JournalError(f"{method} {path} -> {e.code}")
            except Exception as e:  # network
                last = JournalError(f"{method} {path} failed: {type(e).__name__}")
            time.sleep(0.5 * (attempt + 1))
        raise last or JournalError("request failed")

    def _uid(self) -> str:
        return f"eq.{self.uid}"

    # ---- config ----
    def get_config(self) -> Optional[dict]:
        r = self._req("GET", "scalper_config", {"select": "config,version", "user_id": self._uid()})
        return r[0]["config"] if r else None

    def save_config(self, cfg: dict) -> None:
        self._req("POST", "scalper_config", {"on_conflict": "user_id"}, {"user_id": self.uid, "config": cfg},
                  "resolution=ignore-duplicates,return=minimal")

    # ---- commands ----
    def expire_pending_commands(self) -> int:
        r = self._req("PATCH", "scalper_commands", {"user_id": self._uid(), "status": "in.(pending,processing)"},
                      {"status": "rejected", "result": {"error": "engine restarted; command discarded for safety"},
                       "processed_at": now_iso()}, "return=representation")
        return len(r or [])

    def claim_commands(self, ttl_sec: float) -> list[dict]:
        rows = self._req("GET", "scalper_commands", {"select": "*", "user_id": self._uid(), "status": "eq.pending",
                                                     "order": "created_at.asc", "limit": "10"}) or []
        claimed = []
        for row in rows:
            age = time.time() - datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()
            if age > ttl_sec:
                self.finish_command(row["id"], "rejected", {"error": f"command expired ({int(age)}s old)"})
                continue
            got = self._req("PATCH", "scalper_commands", {"id": f"eq.{row['id']}", "status": "eq.pending"},
                            {"status": "processing"}, "return=representation")
            if got:
                claimed.append(got[0])
        return claimed

    def finish_command(self, cid: str, status: str, result: dict) -> None:
        self._req("PATCH", "scalper_commands", {"id": f"eq.{cid}"}, {"status": status, "result": result, "processed_at": now_iso()})

    # ---- state ----
    def upsert_state(self, row: dict) -> None:
        self._req("POST", "scalper_state", {"on_conflict": "user_id"}, {**row, "user_id": self.uid, "updated_at": now_iso()},
                  "resolution=merge-duplicates,return=minimal", retries=0)

    def get_state(self) -> Optional[dict]:
        r = self._req("GET", "scalper_state", {"select": "*", "user_id": self._uid()})
        return r[0] if r else None

    # ---- signals / executions ----
    def insert_signal(self, row: dict) -> bool:
        r = self._req("POST", "scalper_signals", {"on_conflict": "user_id,signal_id"}, {**row, "user_id": self.uid},
                      "resolution=ignore-duplicates,return=representation")
        return bool(r)

    def update_signal(self, signal_id: str, outcome: str, reject_reason: Optional[str] = None) -> None:
        body: dict[str, Any] = {"outcome": outcome}
        if reject_reason is not None:
            body["reject_reason"] = reject_reason[:300]
        self._req("PATCH", "scalper_signals", {"user_id": self._uid(), "signal_id": f"eq.{signal_id}"}, body)

    def upsert_execution(self, row: dict) -> None:
        self._req("POST", "scalper_executions", {"on_conflict": "user_id,signal_id,mode"},
                  {**row, "user_id": self.uid, "updated_at": now_iso()}, "resolution=merge-duplicates,return=minimal")

    def get_execution(self, signal_id: str, mode: str) -> Optional[dict]:
        r = self._req("GET", "scalper_executions", {"select": "*", "user_id": self._uid(), "signal_id": f"eq.{signal_id}", "mode": f"eq.{mode}"})
        return r[0] if r else None

    def unresolved_executions(self, mode: str) -> list[dict]:
        return self._req("GET", "scalper_executions", {"select": "*", "user_id": self._uid(), "mode": f"eq.{mode}",
                                                       "state": "in.(INTENT,SENT,AMBIGUOUS)"}) or []

    # ---- positions ----
    def upsert_position(self, row: dict) -> None:
        self._req("POST", "scalper_positions", {"on_conflict": "user_id,mode,ticket"},
                  {**row, "user_id": self.uid, "updated_at": now_iso()}, "resolution=merge-duplicates,return=minimal")

    def positions(self, mode: str, status: str) -> list[dict]:
        return self._req("GET", "scalper_positions", {"select": "*", "user_id": self._uid(), "mode": f"eq.{mode}", "status": f"eq.{status}",
                                                      "order": "opened_at.desc", "limit": "200"}) or []

    def closed_since(self, mode: str, since_iso: str) -> list[dict]:
        return self._req("GET", "scalper_positions", {"select": "*", "user_id": self._uid(), "mode": f"eq.{mode}", "status": "eq.closed",
                                                      "closed_at": f"gte.{since_iso}", "order": "closed_at.asc", "limit": "1000"}) or []

    # ---- risk / audit ----
    def add_risk_event(self, severity: str, kind: str, message: str, details: dict) -> None:
        self._req("POST", "scalper_risk_events", None, {"user_id": self.uid, "severity": severity, "kind": kind,
                                                        "message": message[:500], "details": details})

    def unresolved_critical(self) -> list[dict]:
        return self._req("GET", "scalper_risk_events", {"select": "*", "user_id": self._uid(), "severity": "eq.critical",
                                                        "resolved_at": "is.null", "limit": "20"}) or []

    def resolve_risk_events(self) -> None:
        self._req("PATCH", "scalper_risk_events", {"user_id": self._uid(), "resolved_at": "is.null"}, {"resolved_at": now_iso()})

    def audit(self, actor: str, action: str, details: dict) -> None:
        self._req("POST", "rpc/scalper_audit_append", None, {"_user": self.uid, "_actor": actor, "_action": action, "_details": details})


class MemoryJournal(Journal):
    """In-memory journal with the same semantics (dedupe, upsert keys) for tests."""

    def __init__(self) -> None:
        self.config: Optional[dict] = None
        self.commands: list[dict] = []
        self.state: Optional[dict] = None
        self.signals: dict[str, dict] = {}
        self.executions: dict[tuple[str, str], dict] = {}
        self.pos: dict[tuple[str, int], dict] = {}
        self.events: list[dict] = []
        self.audits: list[dict] = []
        self.fail_writes = False  # test hook: simulate a database outage

    def _check(self) -> None:
        if self.fail_writes:
            raise JournalError("simulated outage")

    def get_config(self): return self.config
    def save_config(self, cfg): self.config = cfg

    def claim_commands(self, ttl_sec):
        out = []
        for c in self.commands:
            if c["status"] == "pending":
                if time.time() - c.get("ts", time.time()) > ttl_sec:
                    c["status"], c["result"] = "rejected", {"error": "expired"}
                    continue
                c["status"] = "processing"
                out.append(c)
        return out

    def finish_command(self, cid, status, result):
        for c in self.commands:
            if c["id"] == cid:
                c["status"], c["result"] = status, result

    def expire_pending_commands(self):
        n = 0
        for c in self.commands:
            if c["status"] in ("pending", "processing"):
                c["status"], c["result"] = "rejected", {"error": "engine restarted"}
                n += 1
        return n

    def upsert_state(self, row): self._check(); self.state = {**(self.state or {}), **row}
    def get_state(self): return self.state

    def insert_signal(self, row):
        self._check()
        if row["signal_id"] in self.signals:
            return False
        self.signals[row["signal_id"]] = dict(row)
        return True

    def update_signal(self, signal_id, outcome, reject_reason=None):
        if signal_id in self.signals:
            self.signals[signal_id]["outcome"] = outcome
            if reject_reason is not None:
                self.signals[signal_id]["reject_reason"] = reject_reason

    def upsert_execution(self, row):
        self._check()
        k = (row["signal_id"], row["mode"])
        self.executions[k] = {**self.executions.get(k, {}), **row}

    def get_execution(self, signal_id, mode): return self.executions.get((signal_id, mode))

    def unresolved_executions(self, mode):
        return [e for (s, m), e in self.executions.items() if m == mode and e.get("state") in ("INTENT", "SENT", "AMBIGUOUS")]

    def upsert_position(self, row):
        k = (row["mode"], row["ticket"])
        self.pos[k] = {**self.pos.get(k, {}), **row}

    def positions(self, mode, status): return [dict(p) for (m, _), p in self.pos.items() if m == mode and p.get("status") == status]

    def closed_since(self, mode, since_iso):
        return [dict(p) for (m, _), p in self.pos.items() if m == mode and p.get("status") == "closed" and (p.get("closed_at") or "") >= since_iso]

    def add_risk_event(self, severity, kind, message, details):
        self.events.append({"severity": severity, "kind": kind, "message": message, "details": details, "resolved_at": None})

    def unresolved_critical(self): return [e for e in self.events if e["severity"] == "critical" and not e["resolved_at"]]

    def resolve_risk_events(self):
        for e in self.events:
            e["resolved_at"] = now_iso()

    def audit(self, actor, action, details): self.audits.append({"actor": actor, "action": action, "details": details})

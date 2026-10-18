"""Read-only operations API (stdlib only): health for supervisors and status for the operator.

Trading commands are deliberately NOT exposed here. They arrive only through the authenticated, RLS-gated
Supabase command queue so every action is tied to a logged-in dashboard user and written to the audit chain.
Bound to 127.0.0.1 by default; put it behind your VPS firewall / reverse proxy with TLS if exposed.
"""
from __future__ import annotations

import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "ForexAI Scalper Engine Ops API", "version": "1.0.0"},
    "paths": {
        "/health": {"get": {"summary": "Liveness/readiness for the process supervisor (no auth, no sensitive data)",
                            "responses": {"200": {"description": "engine healthy"}, "503": {"description": "degraded or disconnected"}}}},
        "/status": {"get": {"summary": "Engine mode, health checks and counters", "security": [{"bearer": []}],
                            "responses": {"200": {"description": "status"}, "401": {"description": "bad or missing token"}}}},
        "/openapi.json": {"get": {"summary": "This document"}},
    },
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
}


def make_server(engine: "Engine", host: str, port: int, token: str) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # keep request lines out of the logs
            return

        def _send(self, code: int, body: Any) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/openapi.json":
                return self._send(200, OPENAPI)
            if self.path == "/health":
                alive = all(t.is_alive() for t in engine.threads) if engine.threads else True
                ok = engine.connected and alive and not engine.stop_event.is_set()
                return self._send(200 if ok else 503, {"status": "ok" if ok else "degraded", "connected": engine.connected, "loops_alive": alive})
            if self.path == "/status":
                auth = self.headers.get("Authorization", "")
                if not token or not hmac.compare_digest(auth, f"Bearer {token}"):
                    return self._send(401, {"error": "unauthorized"})
                return self._send(200, {
                    "mode": engine.compute_mode(), "connected": engine.connected, "locked": engine.locked, "lock_reason": engine.lock_reason,
                    "paused": engine.paused, "exec_mode": engine.exec_mode, "analysis_running": engine.analysis_running,
                    "open_live": len(engine.live_pos), "open_paper": len(engine.paper_pos), "health": engine.health.snapshot(),
                    "recent_alerts": engine.alerts.recent[-10:], "time": time.time(),
                })
            self._send(404, {"error": "not found"})

    return ThreadingHTTPServer((host, port), H)


def start_api(engine: "Engine", host: str, port: int, token: str) -> ThreadingHTTPServer:
    srv = make_server(engine, host, port, token)
    threading.Thread(target=srv.serve_forever, name="ops-api", daemon=True).start()
    return srv

"""Entry point: python -m scalper_engine"""
from __future__ import annotations

import logging
import os
import signal
import sys

from .api import start_api
from .config import ENGINE_VERSION, EnvSettings
from .engine import Engine
from .monitoring_and_alerts import Alerts, setup_logging
from .mt5_connection import MT5Broker
from .trade_journal import SupabaseJournal


def main() -> int:
    env = EnvSettings.from_env()
    os.makedirs(env.state_dir, exist_ok=True)
    setup_logging(env.log_level, env.secrets(), os.path.join(env.state_dir, "engine.log"))
    log = logging.getLogger("main")
    errors = env.validate()
    if errors:
        for e in errors:
            log.error("configuration error: %s", e)
        return 2
    log.info("starting ForexAI Scalper engine %s (live orders %s on this VPS)", ENGINE_VERSION, "ALLOWED" if env.allow_live else "DISABLED")
    broker = MT5Broker(env.mt5_path, env.mt5_login, env.mt5_password, env.mt5_server)
    journal = SupabaseJournal(env.supabase_url, env.service_key, env.user_id)
    alerts = Alerts(journal, env.telegram_token, env.telegram_chat_id)
    engine = Engine(env, broker, journal, alerts)
    if env.api_token:
        start_api(engine, env.api_host, env.api_port, env.api_token)
    else:
        start_api(engine, env.api_host, env.api_port, "")  # /health only; /status stays locked without a token
    signal.signal(signal.SIGINT, lambda *_: engine.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: engine.stop())
    try:
        engine.run()
    finally:
        broker.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

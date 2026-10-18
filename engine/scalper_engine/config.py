"""Environment settings, default trading configuration and hard safety caps.

Two layers of configuration:
  * EnvSettings: secrets and hard caps, read from the VPS environment only. The dashboard can never raise a
    hard cap and never sees a secret.
  * Trading config: JSON stored in Supabase (scalper_config), edited from the dashboard, merged over the
    defaults below and clamped to the hard caps before use.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any, Optional

ENGINE_VERSION = "1.0.0"
STRATEGY_VERSION = "ema-pullback-scalp-1.0"


def _bool(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class EnvSettings:
    supabase_url: str = ""
    service_key: str = field(default="", repr=False)
    user_id: str = ""
    mt5_path: str = ""
    mt5_login: int = 0
    mt5_password: str = field(default="", repr=False)
    mt5_server: str = ""
    allow_live: bool = False  # hard switch: live orders are impossible unless this is true on the VPS
    allow_real_account: bool = False  # second switch: REAL (non-demo) accounts
    hard_max_risk_pct: float = 1.0
    hard_max_daily_loss_pct: float = 5.0
    hard_max_positions: int = 5
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    api_token: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    telegram_control: bool = True  # let the authorised Telegram chat run the SAFE commands (status, pause, stop)
    ping_url: str = field(default="", repr=False)  # dead-man's-switch URL, pinged only while the engine is healthy
    log_level: str = "INFO"
    state_dir: str = "./state"
    command_poll_sec: float = 1.5
    command_ttl_sec: float = 90.0
    heartbeat_sec: float = 2.0
    stale_heartbeat_sec: float = 15.0

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "EnvSettings":
        e = env if env is not None else os.environ
        g = e.get
        return cls(
            supabase_url=(g("SUPABASE_URL") or "").rstrip("/"),
            service_key=g("SUPABASE_SERVICE_ROLE_KEY") or "",
            user_id=g("ENGINE_USER_ID") or "",
            mt5_path=g("MT5_PATH") or "",
            mt5_login=int(g("MT5_LOGIN") or 0),
            mt5_password=g("MT5_PASSWORD") or "",
            mt5_server=g("MT5_SERVER") or "",
            allow_live=_bool(g("ENGINE_ALLOW_LIVE")),
            allow_real_account=_bool(g("ENGINE_ALLOW_REAL_ACCOUNT")),
            hard_max_risk_pct=float(g("HARD_MAX_RISK_PER_TRADE_PCT") or 1.0),
            hard_max_daily_loss_pct=float(g("HARD_MAX_DAILY_LOSS_PCT") or 5.0),
            hard_max_positions=int(g("HARD_MAX_TOTAL_POSITIONS") or 5),
            api_host=g("ENGINE_API_HOST") or "127.0.0.1",
            api_port=int(g("ENGINE_API_PORT") or 8765),
            api_token=g("ENGINE_API_TOKEN") or "",
            telegram_token=g("TELEGRAM_BOT_TOKEN") or "",
            telegram_chat_id=g("TELEGRAM_CHAT_ID") or "",
            telegram_control=_bool(g("TELEGRAM_CONTROL"), True),
            ping_url=g("HEALTHCHECK_PING_URL") or "",
            log_level=(g("LOG_LEVEL") or "INFO").upper(),
            state_dir=g("ENGINE_STATE_DIR") or "./state",
        )

    def secrets(self) -> list[str]:
        return [s for s in (self.service_key, self.mt5_password, self.api_token, self.telegram_token, self.ping_url) if s]

    def validate(self) -> list[str]:
        errs = []
        if not self.supabase_url.startswith("https://"):
            errs.append("SUPABASE_URL must be an https URL")
        if not self.service_key:
            errs.append("SUPABASE_SERVICE_ROLE_KEY is missing")
        if len(self.user_id) < 30:
            errs.append("ENGINE_USER_ID must be the owner's auth user uuid")
        if self.mt5_login <= 0:
            errs.append("MT5_LOGIN (the expected account number) is required so the engine can verify the account")
        return errs


_INSTRUMENT_DEFAULTS: dict[str, Any] = {
    "XAUUSD": {
        "enabled": True,
        "broker_symbol": "XAUUSD",
        "max_spread_points": 35,
        "commission_per_lot": 0.0,  # account currency, round turn; set from your broker's contract sheet
        "slippage_points": 8,
        "max_lots": 1.0,
        "sessions_utc": [[7, 20]],  # London + New York; [] means always
        "strategy": {"sl_atr_mult": 1.3, "min_body_atr": 0.30},
        "trail": {
            "mode": "atr", "activation_atr": 1.0, "distance_atr": 1.0, "fixed_points": 150,
            "min_step_points": 5, "break_even": True, "break_even_atr": 0.8, "break_even_offset_points": 5,
        },
    },
    "BTCUSD": {
        "enabled": True,
        "broker_symbol": "BTCUSD",
        "max_spread_points": 3500,
        "commission_per_lot": 0.0,
        "slippage_points": 600,
        "max_lots": 0.5,
        "sessions_utc": [],
        "strategy": {"sl_atr_mult": 1.5, "min_body_atr": 0.35},
        "trail": {
            "mode": "atr", "activation_atr": 1.2, "distance_atr": 1.2, "fixed_points": 6000,
            "min_step_points": 300, "break_even": True, "break_even_atr": 0.9, "break_even_offset_points": 300,
        },
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "strategy": {
        "ema_fast": 9, "ema_mid": 21, "ema_slow": 50, "rsi_period": 14, "atr_period": 14,
        "rsi_buy_min": 52, "rsi_buy_max": 72, "rsi_sell_min": 28, "rsi_sell_max": 48,
        "min_body_atr": 0.30,           # last candle body must be at least this many ATRs (momentum)
        "max_extension_atr": 1.2,       # do not chase: close may be at most this far from EMA21
        "sl_atr_mult": 1.3, "max_sl_atr_mult": 3.0, "swing_lookback": 8, "swing_buffer_atr": 0.1,
        "tp_rr": 1.6,
        "max_atr_ratio": 2.5,           # abnormal volatility: current ATR vs its 50-bar mean
        "min_atr_spread_ratio": 4.0,    # ATR must be at least this multiple of the spread
        "use_m15": False,
        "signal_ttl_sec": 45,           # a signal is only actionable this long after its candle closed
        "max_bar_lag_sec": 150,         # last completed candle must be recent relative to the quote
        "max_tick_age_sec": 15,
        "bars_m1": 300, "bars_m5": 300, "bars_m15": 200,
    },
    "risk": {
        "risk_per_trade_pct": 0.25,
        "max_daily_loss_pct": 2.0,
        "max_aggregate_open_risk_pct": 0.75,
        "max_total_positions": 2,
        "max_positions_per_symbol": 1,
        "max_consecutive_losses": 3,
        "max_margin_utilization_pct": 30,
        "min_net_rr": 1.1,
        "max_slippage_points_factor": 1.0,   # multiplier on each instrument's slippage_points for the order deviation
        "max_session_exposure_pct": 1.5,     # total risk opened in one UTC day
        "max_trades_per_hour": 6,
        "cooldown_after_loss_sec": 300,
        "breach_policy": "pause",            # 'pause' (keep managing open trades) | 'close_all'
    },
    "execution": {"magic": 20260117, "comment_prefix": "FAIP", "max_retries": 2, "stop_retries": 3},
    "paper": {"start_balance": 10000.0, "slippage_points_factor": 1.0},
    "instruments": _INSTRUMENT_DEFAULTS,
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_config() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def effective_config(stored: Optional[dict], env: EnvSettings) -> tuple[dict[str, Any], list[str]]:
    """Merge stored config over defaults and clamp to the VPS hard caps. Returns (config, warnings)."""
    cfg = deep_merge(DEFAULT_CONFIG, stored or {})
    warns: list[str] = []
    r = cfg["risk"]

    def clamp(key: str, hi: float, lo: float = 0.0) -> None:
        v = float(r[key])
        nv = min(max(v, lo), hi)
        if nv != v:
            warns.append(f"risk.{key} {v} clamped to {nv}")
        r[key] = nv

    clamp("risk_per_trade_pct", env.hard_max_risk_pct, 0.01)
    clamp("max_daily_loss_pct", env.hard_max_daily_loss_pct, 0.1)
    clamp("max_aggregate_open_risk_pct", env.hard_max_daily_loss_pct, 0.01)
    r["max_total_positions"] = int(min(max(int(r["max_total_positions"]), 1), env.hard_max_positions))
    r["max_positions_per_symbol"] = int(min(max(int(r["max_positions_per_symbol"]), 1), r["max_total_positions"]))
    if r.get("breach_policy") not in ("pause", "close_all"):
        r["breach_policy"] = "pause"
        warns.append("risk.breach_policy reset to pause")
    for name, inst in cfg["instruments"].items():
        if not str(inst.get("broker_symbol", "")).strip():
            inst["enabled"] = False
            warns.append(f"{name} has no broker symbol and was disabled")
    return cfg, warns


def instrument_strategy(cfg: dict, instrument: str) -> dict:
    """Global strategy settings with the instrument's own overrides (tuned independently per instrument)."""
    return deep_merge(cfg["strategy"], cfg["instruments"].get(instrument, {}).get("strategy", {}))

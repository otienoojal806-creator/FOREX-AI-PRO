# Architecture

```
 PHONE (React PWA, Netlify)                SUPABASE                          WINDOWS VPS
 ┌──────────────────────┐   RPC (RLS,    ┌───────────────────────┐   poll    ┌────────────────────────────┐
 │ Scalper page         │──module check─▶│ scalper_commands      │◀─────────│ command_pass (1.5 s)       │
 │  controls, settings  │                │ scalper_config        │──config──▶│ Engine                      │
 │  live view           │◀──Realtime─────│ scalper_state         │◀──state──│  analysis_pass (each M1)   │
 └──────────────────────┘                │ scalper_signals       │           │  manage_pass  (ticks)      │
        browser never sees               │ scalper_executions    │◀─journal─│  state_pass / watchdog     │
        MT5 or broker secrets            │ scalper_positions     │           │ MT5 gateway (live | paper) │
                                         │ scalper_risk_events   │           └──────────┬─────────────────┘
                                         │ scalper_audit (chain) │                      │ MetaTrader5 package
                                         └───────────────────────┘                      ▼
                                                                              MT5 terminal ─▶ Broker
```

| Layer | Where | Role |
|---|---|---|
| Frontend | `src/pages/Scalper.tsx` | Reads state through RLS-gated tables, sends commands through `scalper_send_command`. Holds no secrets. |
| Application backend | Supabase RPC + RLS (`0005_scalper.sql`) | Auth, module check, validation, audit chain, command queue, Realtime. |
| Trading engine | `engine/scalper_engine` | **Only** component that can place orders. Outbound-only. Service key stays on the VPS. |
| Storage | Supabase PostgreSQL | Config, signals, executions, positions (trailing recovery state), risk events, tamper-evident audit. |

Engine modules: `mt5_connection`, `market_data`, `symbol_discovery`, `indicators`, `strategy_engine`, `risk_manager`,
`order_executor`, `trailing_stop_manager`, `position_reconciler`, `trade_journal`, `monitoring_and_alerts`, `gateways` (live/paper), `backtest`.

## Safety design
- **Starts paused**, live disarmed, after every restart or deploy. The supervisor can only restart the process, never enable entries.
- **Two VPS switches** (`ENGINE_ALLOW_LIVE`, `ENGINE_ALLOW_REAL_ACCOUNT`) and hard caps (`HARD_MAX_*`) live in the VPS environment; the app cannot raise them.
- **Arming live** needs: engine pre-check pass, a one-time preview token (2 min), the typed phrase `ARM LIVE` (re-checked by the database), and a matching account login. It arms *paused*; you then tap Resume.
- **Signals are idempotent**: unique `(user, signal_id)`; one candle can never create two entries. Ambiguous order timeouts are reconciled by unique order comment before any retry, and trading locks if the outcome stays unknown.
- **Stops**: every entry needs a broker-side SL. Fill without SL triggers recovery, then close, then lock. Trailing only tightens.
- **Dashboard honesty**: the app shows `ENGINE OFFLINE` when the heartbeat is older than 15 s instead of the last stored mode. `LIVE ACTIVE` is only reported by the engine after its own health checks.
- **Emergency stop** disables entries and disarms live but does not close trades. Closing needs the typed phrase `CLOSE`.

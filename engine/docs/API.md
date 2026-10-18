# Commands and API

## Dashboard to engine (Supabase RPC)
`rpc('scalper_send_command', {_command, _payload})` returns a command id. The engine answers by updating the row in
`scalper_commands` (`status`: done / rejected / failed, `result`). Requires the Automated Trading module, max 30 commands per minute, every command is audited.

| Command | Payload | Effect |
|---|---|---|
| connect | – | (Re)connect to MT5, verify account, discover symbols |
| discover_symbols | – | Lists broker symbol candidates for gold / bitcoin |
| start_analysis / stop_analysis | – | Analysis only (no orders). Stop also pauses entries |
| start_paper | – | Simulated fills on real quotes |
| request_live_preview | – | Runs pre-arm checks; returns account, risk, checks and a 2-minute `preview_token` |
| arm_live | `preview_token`, `confirm_text: "ARM LIVE"` | Arms live, **entries stay paused** |
| disarm_live | – | Disarms live; open trades keep being managed |
| pause_entries / resume_entries | – | Toggle new entries |
| emergency_stop | – | Entries off, live disarmed, trades NOT closed |
| close_positions | `mode: all|paper|live`, `confirm_text: "CLOSE"` | Market-closes bot trades |
| unlock_trading | `confirm_text: "UNLOCK"` | Clears a risk lock (refused while any trade lacks a stop) |
| reconcile | – | Rebuild state from broker positions |

`rpc('scalper_save_config', {_config})` saves settings (range-checked in the database and clamped again on the VPS). Only admins can change broker-symbol mapping.
`rpc('scalper_verify_audit_chain')` returns false if audit rows were altered.

## Tables the app reads (RLS: owner with Automated Trading)
`scalper_state` (live status, quotes, latest analyses, health), `scalper_positions`, `scalper_signals`, `scalper_executions`, `scalper_risk_events`, `scalper_audit`, `scalper_commands`, `scalper_config`. Nobody can write them from the browser.

## Engine ops API (VPS, localhost only)
`GET /health` (no auth, 200 ok / 503 degraded), `GET /status` (Bearer `ENGINE_API_TOKEN`), `GET /openapi.json`. Read-only; trading is never exposed here.

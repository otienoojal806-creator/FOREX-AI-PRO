# Operator runbook

## 1. Daily check (from your phone, 1 minute)
Open Scalper. Status bar should show `PAPER` or `LIVE ACTIVE` with "engine Ns ago" under 15 s and MT5 ok. Log tab: no unresolved red items. Trades tab: every trade shows an SL.

**Locked phone:** message your Telegram bot `/status` to check, `/pause` to stop new entries, `/stop` for an emergency stop (open trades stay protected). If the dead-man's-switch alert fires (no pings for ~5 min), assume the VPS, MT5 or the engine is down: open trades rely on their broker-side stop-loss only. Log in to the VPS (or your provider's console from your phone) and restart the service.

## 2. Something is wrong
| You see | Do |
|---|---|
| ENGINE OFFLINE | VPS or engine down. Open trades rely on broker stops. Check the VPS (RDP), run `deploy\healthcheck.ps1`, check `state\engine.log` and `state\supervisor.log`. After restart the bot is PAUSED: Connect, then resume only after you've looked at open trades |
| Trading locked | Read the reason. Check MT5 and that every position has a stop. Fix, then Unlock (type UNLOCK), then Resume |
| NO STOP LOSS badge | Open MT5 now and set a stop manually or close the trade. Then tap Reconcile |
| Ambiguous order | Engine locks entries. Check MT5 history for the order, then Unlock |
| Panic | Tap STOP (no new entries). To flatten: Close bot trades (type CLOSE). Or close in the MT5 phone app |

## 3. Acceptance tests before live (do on a broker DEMO account)
1. Paper for 1+ week: signals deduplicated, no entries outside sessions, spread filter working.
2. Demo live (arm on demo): confirm fills show in MT5 with SL. Compare lots with your own risk maths.
3. Trailing: watch a profitable demo trade; the SL must only ever move toward price. Try a manual SL edit in MT5 and confirm the engine never loosens it.
4. Restart test: kill the engine with a trade open. Broker SL stays. Restart: engine adopts the trade, trailing resumes from stored high/low, state is PAUSED.
5. Disconnect test: stop the MT5 terminal then restart it; confirm `ENGINE OFFLINE`/disconnected shows, recovery works, no duplicate orders.
6. Rejection test: set tiny max spread; confirm entries are rejected with a reason and no fill is claimed.
7. Run backtests per instrument with out-of-sample and walk-forward (docs/BACKTESTING.md).

## 4. Going live
Set `ENGINE_ALLOW_LIVE=true` (and `ENGINE_ALLOW_REAL_ACCOUNT=true` for a real account) in `.env`, restart the task, start with the smallest risk (0.25% or less) and minimum lots. In the app: Arm live, read the account and warnings, type `ARM LIVE`, then Resume. Never leave it unattended in the first weeks.

## 5. Maintenance
- Update: stop task, replace files, `.\deploy\install.ps1`, start task.
- Rotate the Supabase service key if the VPS is ever exposed.
- Keep Windows, MT5 and Python patched; restrict RDP to your IP.
- Logs: `state\engine.log` (secrets are masked).

# ForexAI Scalper (MT5 engine)

Python trading engine for XAUUSD and BTCUSD scalping on a MetaTrader 5 account, controlled from the ForexAI Pro
web app (made to be used from a phone). **It starts PAUSED on every launch, defaults to PAPER, and never trades live
until you arm it.**

```
Phone / browser  -->  Supabase (RLS + RPC + Realtime)  <--  Engine on Windows VPS  -->  MT5 terminal  -->  Broker
(React app)           commands, config, state, audit       (outbound only, service key)   (same machine)
```

Docs: [Architecture](docs/ARCHITECTURE.md) · [Commands and API](docs/API.md) · [Operator runbook](docs/RUNBOOK.md) · [Backtesting](docs/BACKTESTING.md)

## Why a Windows machine is required
The official `MetaTrader5` Python package only talks to a **running MT5 desktop terminal on Windows**. Netlify and
your phone cannot run it. Rent a small Windows VPS (2 vCPU / 4 GB is plenty), install MT5 from your broker and keep it logged in.
The web app stays on Netlify; the engine on the VPS only makes outbound HTTPS calls to Supabase, so no inbound ports are opened.

## Setup (about 30 minutes)
1. **Database**: apply `supabase/migrations/0005_scalper.sql` to your Supabase project (once).
2. **VPS**: install MT5 from your broker, log in to a **demo** account, enable *Algo Trading*. Install 64-bit Python 3.10+ ("Add to PATH").
3. Copy the `engine` folder to the VPS, then in PowerShell: `.\deploy\install.ps1` (creates venv, installs, runs the tests).
4. `notepad .env` and fill in: `SUPABASE_SERVICE_ROLE_KEY` (Supabase > Settings > API, **secret key, VPS only**),
   `ENGINE_USER_ID` (your user's id in Supabase > Authentication), `MT5_PATH`, `MT5_LOGIN`, `MT5_SERVER`, optional Telegram alert keys.
5. `.\deploy\check_env.ps1`, then `.\deploy\install_service.ps1` (starts at logon and restarts after crashes, always PAUSED).
6. On your phone open the app, **Scalper** tab, tap **Connect MT5**, then **Start analysis**, then **Start paper**. Use your browser menu "Add to Home screen" to install it.
7. Run paper and **broker-demo** trading for at least a couple of weeks. Check stops, trailing and restarts (runbook section 3).
8. Only then consider live: set `ENGINE_ALLOW_LIVE=true` (and `ENGINE_ALLOW_REAL_ACCOUNT=true` for a real account), restart, and use **Arm live…** in the app.

## Phone alerts and remote control (works with the screen locked)
- In the app, **Overview > Critical alerts** buzzes and notifies while the app is open.
- **Telegram** (recommended): create a bot with @BotFather, message it once, then set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.
  The engine messages you on disconnects, missing stops, risk locks and rejected orders, and answers these commands from **your chat only**:
  `/status`, `/positions`, `/today`, `/pause` (pause new entries) and `/stop` (emergency stop: entries off, live disarmed, open trades stay protected).
  It can never resume, arm live, unlock, change settings or close trades; those need the app and its typed confirmations. Set `TELEGRAM_CONTROL=false` for alerts only.
- **Dead-man's switch** (hears about a dead VPS): create a free check at healthchecks.io (period 2 min, grace 3 min), put its ping URL in `HEALTHCHECK_PING_URL`.
  The engine pings it only while MT5, the database and all trading loops are healthy, so if anything dies or hangs the pings stop and you get an alert on your phone.
  Nothing else can tell you the engine itself is down, because a dead engine cannot send a message.

## Tests
`python -m unittest discover -s tests -t .` (offline; uses a fake broker, no MT5 needed). Integration against real MT5 can only be done on your VPS with a demo account (runbook).

## Honest limits
- Nothing here has been run against a real MT5 account by the author. Treat everything as **unverified until your own demo tests pass**.
- A trailing stop managed by the engine only works while the VPS, the MT5 terminal and the internet are up. The **broker-side stop-loss** placed with every trade is what protects you otherwise.
- Backtests and paper results do not guarantee live results. Scalping is sensitive to spread, slippage and latency, and most retail traders lose money.

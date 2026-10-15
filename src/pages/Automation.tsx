import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, FlaskConical, Plug, ShieldCheck, Zap } from 'lucide-react'
import { supabase } from '../lib/supabase'
import { SYMBOLS, callFn, decimals } from '../lib/api'
import { DirBadge, ErrorBox, Notice, PageHeader, fmtDate, money } from '../components/ui'

type Mode = 'simulated' | 'live'
interface Settings {
  mode: Mode; lot_size: number; risk_pct: number; sl_pips: number; tp_pips: number; trailing_pips: number
  max_open_trades: number; min_confidence: number; symbols: string[]; auto_enabled: boolean
  live_consent_at: string | null; broker_connected: boolean
}
interface Trade {
  id: string; mode: Mode; symbol: string; direction: 'BUY' | 'SELL'; lots: number; entry_price: number
  sl: number; tp: number; trailing_pips: number; status: 'pending' | 'open' | 'closed'
  close_price: number | null; pnl_usd: number | null; close_reason: string | null
  opened_at: string; closed_at: string | null
}
interface Live { id: string; price: number; sl: number; unrealisedUsd: number }
interface Summary { simBalance: number; mode: Mode; live?: { balance: number; equity: number; currency: string } }

const DEFAULTS: Settings = {
  mode: 'simulated', lot_size: 0.01, risk_pct: 1, sl_pips: 30, tp_pips: 60, trailing_pips: 0,
  max_open_trades: 3, min_confidence: 70, symbols: ['EUR/USD'], auto_enabled: false,
  live_consent_at: null, broker_connected: false,
}

function ModeBadge({ mode }: { mode: Mode }) {
  return mode === 'live'
    ? <span className="badge bg-sell/20 text-red-300">LIVE</span>
    : <span className="badge bg-accent/20 text-blue-300">SIM</span>
}

export default function Automation() {
  const [saved, setSaved] = useState<Settings | null>(null)
  const [form, setForm] = useState<Settings>(DEFAULTS)
  const [trades, setTrades] = useState<Trade[]>([])
  const [live, setLive] = useState<Record<string, Live>>({})
  const [summary, setSummary] = useState<Summary | null>(null)
  const [scanLog, setScanLog] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [ack, setAck] = useState(false)
  const [broker, setBroker] = useState({ accountId: '', token: '', region: 'new-york' })
  const [manualSymbol, setManualSymbol] = useState('EUR/USD')
  const monitoring = useRef(false)
  const scanIdx = useRef(0)

  const settings = saved ?? DEFAULTS
  const liveReady = !!settings.live_consent_at && settings.broker_connected

  const loadSettings = useCallback(async () => {
    const { data, error } = await supabase.from('automation_settings').select('*').maybeSingle()
    if (error) { setErr(error.message); return }
    if (data) {
      const s: Settings = { ...DEFAULTS, ...(data as Settings) }
      setSaved(s); setForm(s)
    }
  }, [])

  const loadTrades = useCallback(async () => {
    const { data, error } = await supabase.from('trades').select('*').order('opened_at', { ascending: false }).limit(100)
    if (error) setErr(error.message); else setTrades((data ?? []) as Trade[])
  }, [])

  const loadSummary = useCallback(async () => {
    try { setSummary(await callFn<Summary>('trade', { action: 'summary' })) } catch { /* needs saved settings first */ }
  }, [])

  useEffect(() => { void loadSettings(); void loadTrades() }, [loadSettings, loadTrades])
  useEffect(() => { if (saved) void loadSummary() }, [saved, loadSummary])

  const open = trades.filter((t) => t.status !== 'closed')
  const closed = trades.filter((t) => t.status === 'closed')
  const openCount = open.length

  // Monitor open trades (stop-loss, take-profit, trailing stop) every 15s while this page is open.
  const monitor = useCallback(async () => {
    if (monitoring.current) return
    monitoring.current = true
    try {
      const res = await callFn<{ open: Live[] }>('trade', { action: 'monitor' })
      const map: Record<string, Live> = {}
      for (const o of res.open) map[o.id] = o
      setLive(map)
      await loadTrades()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Monitoring failed')
    } finally { monitoring.current = false }
  }, [loadTrades])

  useEffect(() => {
    if (openCount === 0) { setLive({}); return }
    void monitor()
    const t = window.setInterval(() => { void monitor() }, 15_000)
    return () => window.clearInterval(t)
  }, [openCount, monitor])

  // Auto-trading: one pair per minute, round-robin, only while this page is open.
  const autoOn = !!saved?.auto_enabled
  const autoSymbols = saved?.symbols.join(',') ?? ''
  useEffect(() => {
    if (!autoOn || !autoSymbols) return
    const list = autoSymbols.split(',')
    const tick = async () => {
      const symbol = list[scanIdx.current % list.length]
      scanIdx.current += 1
      try {
        const r = await callFn<{ action: string; reason?: string; trade?: Trade }>('trade', { action: 'auto_scan', symbol })
        const line = r.action === 'opened' ? `${symbol}: opened a ${r.trade?.direction} trade`
          : `${symbol}: ${r.action}${r.reason ? ` (${r.reason})` : ''}`
        setScanLog((l) => [`${new Date().toLocaleTimeString()}  ${line}`, ...l].slice(0, 20))
        if (r.action === 'opened') { await loadTrades(); void loadSummary() }
      } catch (e) {
        setScanLog((l) => [`${new Date().toLocaleTimeString()}  ${symbol}: ${e instanceof Error ? e.message : 'error'}`, ...l].slice(0, 20))
      }
    }
    const first = window.setTimeout(() => { void tick() }, 5_000)
    const t = window.setInterval(() => { void tick() }, 60_000)
    return () => { window.clearTimeout(first); window.clearInterval(t) }
  }, [autoOn, autoSymbols, loadTrades, loadSummary])

  async function guarded(fn: () => Promise<void>) {
    setBusy(true); setErr(null); setMsg(null)
    try { await fn() } catch (e) { setErr(e instanceof Error ? e.message : 'Something went wrong') } finally { setBusy(false) }
  }

  const num = (v: string, fallback: number) => (v === '' || Number.isNaN(Number(v)) ? fallback : Number(v))

  function saveSettings() {
    // Switching to live, or turning auto-trading on while live, needs an explicit typed confirmation.
    const goingLive = form.mode === 'live' && saved?.mode !== 'live'
    const autoLive = form.mode === 'live' && form.auto_enabled && !(saved?.auto_enabled && saved.mode === 'live')
    if (goingLive || autoLive) {
      const typed = window.prompt(
        'LIVE MODE sends real orders to your broker and real money is at risk.' +
        (form.auto_enabled ? '\nAuto-trading will place orders by itself while this page is open.' : '') +
        '\n\nType LIVE to confirm.')
      if (typed !== 'LIVE') return
    }
    void guarded(async () => {
      const { error } = await supabase.rpc('save_automation_settings', {
        _lot: form.lot_size, _risk: form.risk_pct, _sl: form.sl_pips, _tp: form.tp_pips, _trailing: form.trailing_pips,
        _max_open: form.max_open_trades, _min_conf: form.min_confidence, _symbols: form.symbols,
        _mode: form.mode, _auto: form.auto_enabled,
      })
      if (error) throw new Error(error.message)
      await loadSettings()
      setMsg(form.mode === 'live' ? 'Settings saved. LIVE mode is on.' : 'Settings saved. Simulated mode, no real money.')
    })
  }

  function setConsent(give: boolean) {
    void guarded(async () => {
      const { error } = await supabase.rpc('set_live_consent', { _give: give })
      if (error) throw new Error(error.message)
      setAck(false)
      await loadSettings()
      setMsg(give ? 'Consent recorded. You can now switch to live mode.' : 'Consent revoked. You are back in simulated mode and auto-trading is off.')
    })
  }

  function connectBroker() {
    void guarded(async () => {
      await callFn('trade', { action: 'connect_broker', ...broker })
      setBroker({ accountId: '', token: '', region: broker.region })
      await loadSettings()
      setMsg('Broker connected.')
    })
  }

  function disconnectBroker() {
    if (!window.confirm('Disconnect your broker? Live mode and auto-trading will be switched off.')) return
    void guarded(async () => {
      await callFn('trade', { action: 'disconnect_broker' })
      await loadSettings()
      setMsg('Broker disconnected.')
    })
  }

  function placeTrade(direction: 'BUY' | 'SELL') {
    const isLive = settings.mode === 'live'
    if (isLive && !window.confirm(
      `Place a LIVE ${direction} on ${manualSymbol}?\n\nLots: ${settings.lot_size}\nStop-loss: ${settings.sl_pips} pips\nTake-profit: ${settings.tp_pips} pips\n\nThis uses real money.`)) return
    void guarded(async () => {
      await callFn('trade', { action: 'open', symbol: manualSymbol, direction, confirm: isLive })
      await loadTrades(); void loadSummary()
      setMsg(`${isLive ? 'Live' : 'Simulated'} ${direction} ${manualSymbol} opened.`)
    })
  }

  function closeTrade(t: Trade) {
    if (t.mode === 'live' && !window.confirm(`Close your LIVE ${t.direction} ${t.symbol} now?`)) return
    void guarded(async () => {
      const r = await callFn<{ pnl: number }>('trade', { action: 'close', tradeId: t.id })
      await loadTrades(); void loadSummary()
      setMsg(`Closed. ${t.mode === 'live' ? 'Estimated ' : ''}result: ${money(r.pnl)}`)
    })
  }

  const toggleSymbol = (s: string) =>
    setForm((f) => ({ ...f, symbols: f.symbols.includes(s) ? f.symbols.filter((x) => x !== s) : [...f.symbols, s] }))

  const realised = (m: Mode) => closed.filter((t) => t.mode === m).reduce((a, t) => a + Number(t.pnl_usd ?? 0), 0)

  return (
    <>
      <PageHeader title="Trade Automation" subtitle="Broker connection, risk controls, automatic execution and open-trade monitoring." />

      {/* Mode banner: always visible so simulated and live can never be confused. */}
      {settings.mode === 'live' ? (
        <div className="mb-4 flex items-start gap-3 rounded-xl border border-sell/60 bg-sell/10 p-4">
          <AlertTriangle className="mt-0.5 shrink-0 text-sell-400" size={20} />
          <div>
            <div className="font-bold text-red-200">LIVE MODE: real money</div>
            <div className="text-sm text-red-200/80">Orders are sent to your connected broker account. You can lose money.</div>
          </div>
        </div>
      ) : (
        <div className="mb-4 flex items-start gap-3 rounded-xl border border-accent/50 bg-accent/10 p-4">
          <FlaskConical className="mt-0.5 shrink-0 text-accent-soft" size={20} />
          <div>
            <div className="font-bold text-blue-200">SIMULATED MODE: practice only</div>
            <div className="text-sm text-blue-200/80">A $10,000 practice balance. No broker orders are sent and no real money is used.</div>
          </div>
        </div>
      )}

      <div className="space-y-4">
        <ErrorBox message={err} />
        {msg && <Notice tone="ok">{msg}</Notice>}

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div className="card"><div className="text-xs uppercase text-slate-400">Simulated balance</div><div className="mt-1 text-xl font-bold text-white">{summary ? money(summary.simBalance) : '—'}</div></div>
          <div className="card"><div className="text-xs uppercase text-slate-400">Live equity</div><div className="mt-1 text-xl font-bold text-white">{summary?.live ? `${summary.live.equity} ${summary.live.currency}` : '—'}</div></div>
          <div className="card"><div className="text-xs uppercase text-slate-400">Open trades</div><div className="mt-1 text-xl font-bold text-white">{openCount}</div></div>
          <div className="card"><div className="text-xs uppercase text-slate-400">Realised P&amp;L (sim / live est.)</div>
            <div className="mt-1 text-xl font-bold"><span className={realised('simulated') >= 0 ? 'text-buy' : 'text-sell'}>{money(realised('simulated'))}</span> <span className="text-slate-500">/</span> <span className={realised('live') >= 0 ? 'text-buy' : 'text-sell'}>{money(realised('live'))}</span></div></div>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          {/* Settings */}
          <div className="card space-y-4">
            <h3 className="font-semibold text-white">Risk &amp; execution settings</h3>
            <div className="grid grid-cols-2 gap-3">
              <div><label className="label">Lot size</label><input className="input" type="number" step="0.01" min="0.01" value={form.lot_size} onChange={(e) => setForm({ ...form, lot_size: num(e.target.value, form.lot_size) })} /></div>
              <div><label className="label">Max risk per trade (%)</label><input className="input" type="number" step="0.1" min="0.1" max="10" value={form.risk_pct} onChange={(e) => setForm({ ...form, risk_pct: num(e.target.value, form.risk_pct) })} /></div>
              <div><label className="label">Stop-loss (pips)</label><input className="input" type="number" min="1" value={form.sl_pips} onChange={(e) => setForm({ ...form, sl_pips: num(e.target.value, form.sl_pips) })} /></div>
              <div><label className="label">Take-profit (pips)</label><input className="input" type="number" min="1" value={form.tp_pips} onChange={(e) => setForm({ ...form, tp_pips: num(e.target.value, form.tp_pips) })} /></div>
              <div><label className="label">Trailing stop (pips, 0 = off)</label><input className="input" type="number" min="0" value={form.trailing_pips} onChange={(e) => setForm({ ...form, trailing_pips: num(e.target.value, form.trailing_pips) })} /></div>
              <div><label className="label">Max open trades</label><input className="input" type="number" min="1" max="20" value={form.max_open_trades} onChange={(e) => setForm({ ...form, max_open_trades: num(e.target.value, form.max_open_trades) })} /></div>
              <div className="col-span-2"><label className="label">Minimum signal confidence for auto-trading: {form.min_confidence}%</label><input className="w-full" type="range" min="50" max="95" value={form.min_confidence} onChange={(e) => setForm({ ...form, min_confidence: Number(e.target.value) })} /></div>
            </div>
            <div>
              <label className="label">Pairs for auto-trading</label>
              <div className="flex flex-wrap gap-2">
                {SYMBOLS.map((s) => (
                  <button key={s} type="button" onClick={() => toggleSymbol(s)}
                    className={`rounded-lg border px-2.5 py-1 text-xs font-semibold ${form.symbols.includes(s) ? 'border-accent bg-accent/20 text-blue-200' : 'border-ink-600 text-slate-400'}`}>{s}</button>
                ))}
              </div>
            </div>
            <div>
              <label className="label">Trading mode</label>
              <div className="grid grid-cols-2 gap-2">
                <button type="button" onClick={() => setForm({ ...form, mode: 'simulated' })}
                  className={`rounded-lg border p-3 text-left text-sm ${form.mode === 'simulated' ? 'border-accent bg-accent/15 text-blue-200' : 'border-ink-600 text-slate-400'}`}>
                  <FlaskConical size={16} className="mb-1" /> Simulated<div className="text-xs opacity-70">Practice, no real money</div></button>
                <button type="button" disabled={!liveReady} onClick={() => setForm({ ...form, mode: 'live' })}
                  className={`rounded-lg border p-3 text-left text-sm disabled:cursor-not-allowed disabled:opacity-40 ${form.mode === 'live' ? 'border-sell bg-sell/15 text-red-200' : 'border-ink-600 text-slate-400'}`}>
                  <Zap size={16} className="mb-1" /> Live<div className="text-xs opacity-70">{liveReady ? 'Real money via your broker' : 'Needs broker + consent below'}</div></button>
              </div>
            </div>
            <label className="flex items-start gap-2 text-sm text-slate-300">
              <input type="checkbox" className="mt-1" checked={form.auto_enabled} onChange={(e) => setForm({ ...form, auto_enabled: e.target.checked })} />
              <span>Enable auto-trading. Trades are opened automatically from signals at or above your minimum confidence, <b>only while this page is open</b>.</span>
            </label>
            <button className="btn-primary w-full" disabled={busy || form.symbols.length === 0} onClick={saveSettings}>Save settings</button>
          </div>

          <div className="space-y-4">
            {/* Broker */}
            <div className="card space-y-3">
              <h3 className="flex items-center gap-2 font-semibold text-white"><Plug size={16} /> Broker connection (MT5 via MetaApi)</h3>
              {settings.broker_connected ? (
                <>
                  <Notice tone="ok">Broker connected. Your API token is stored encrypted on the server and is never shown again.</Notice>
                  <button className="btn-ghost" disabled={busy} onClick={disconnectBroker}>Disconnect broker</button>
                </>
              ) : (
                <>
                  <p className="text-xs text-slate-400">Create a MetaApi account linked to your MT5 broker, then enter its account ID and an API token. Prefer a token limited to trading on this account.</p>
                  <div><label className="label">MetaApi account ID</label><input className="input" value={broker.accountId} onChange={(e) => setBroker({ ...broker, accountId: e.target.value })} autoComplete="off" /></div>
                  <div><label className="label">API token</label><input className="input" type="password" value={broker.token} onChange={(e) => setBroker({ ...broker, token: e.target.value })} autoComplete="off" /></div>
                  <div><label className="label">Region</label>
                    <select className="input" value={broker.region} onChange={(e) => setBroker({ ...broker, region: e.target.value })}>
                      <option value="new-york">New York</option><option value="london">London</option><option value="singapore">Singapore</option>
                    </select></div>
                  <button className="btn-primary" disabled={busy || !broker.accountId || !broker.token} onClick={connectBroker}>Connect broker</button>
                </>
              )}
            </div>

            {/* Consent */}
            <div className="card space-y-3">
              <h3 className="flex items-center gap-2 font-semibold text-white"><ShieldCheck size={16} /> Live trading consent</h3>
              {settings.live_consent_at ? (
                <>
                  <Notice tone="ok">Consent recorded on {fmtDate(settings.live_consent_at)}.</Notice>
                  <button className="btn-ghost" disabled={busy} onClick={() => setConsent(false)}>Revoke consent (back to simulated)</button>
                </>
              ) : (
                <>
                  <label className="flex items-start gap-2 text-sm text-slate-300">
                    <input type="checkbox" className="mt-1" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                    <span>I understand live trading uses real money and I can lose some or all of it. I authorise ForexAI Pro to place orders on my connected broker account according to the settings I choose, and I can revoke this at any time.</span>
                  </label>
                  <button className="btn-sell" disabled={busy || !ack} onClick={() => setConsent(true)}>Record my consent</button>
                </>
              )}
            </div>
          </div>
        </div>

        {/* Manual trade */}
        <div className="card flex flex-wrap items-center gap-3">
          <div className="mr-2 font-semibold text-white">Manual trade <ModeBadge mode={settings.mode} /></div>
          <select className="input w-auto" value={manualSymbol} onChange={(e) => setManualSymbol(e.target.value)}>{SYMBOLS.map((s) => <option key={s}>{s}</option>)}</select>
          <button className="btn-buy" disabled={busy || !saved} onClick={() => placeTrade('BUY')}>BUY</button>
          <button className="btn-sell" disabled={busy || !saved} onClick={() => placeTrade('SELL')}>SELL</button>
          {!saved && <span className="text-xs text-slate-500">Save your settings first.</span>}
        </div>

        {/* Open trades */}
        <div className="card p-0">
          <div className="border-b border-ink-600/60 px-4 py-3 font-semibold text-white">Open trades {openCount > 0 && <span className="text-xs font-normal text-slate-500">· refreshed every 15s while this page is open</span>}</div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead><tr><th className="th">Mode</th><th className="th">Pair</th><th className="th">Side</th><th className="th">Lots</th><th className="th">Entry</th><th className="th">SL</th><th className="th">TP</th><th className="th">Price</th><th className="th">Unrealised</th><th className="th" /></tr></thead>
              <tbody>
                {open.length === 0 && <tr><td className="td text-slate-500" colSpan={10}>No open trades.</td></tr>}
                {open.map((t) => {
                  const l = live[t.id]
                  const d = decimals(t.symbol)
                  return (
                    <tr key={t.id} className="border-t border-ink-700">
                      <td className="td"><ModeBadge mode={t.mode} /></td>
                      <td className="td">{t.symbol}</td>
                      <td className="td"><DirBadge dir={t.direction} /></td>
                      <td className="td">{t.lots}</td>
                      <td className="td font-mono">{Number(t.entry_price).toFixed(d)}</td>
                      <td className="td font-mono text-sell">{Number(l?.sl ?? t.sl).toFixed(d)}</td>
                      <td className="td font-mono text-buy">{Number(t.tp).toFixed(d)}</td>
                      <td className="td font-mono">{l ? l.price.toFixed(d) : '…'}</td>
                      <td className={`td font-mono ${l && l.unrealisedUsd < 0 ? 'text-sell' : 'text-buy'}`}>{l ? money(l.unrealisedUsd) : '…'}</td>
                      <td className="td">{t.status === 'pending' ? <span className="text-xs text-amber-300">Placing…</span> : <button className="btn-ghost px-2 py-1 text-xs" disabled={busy} onClick={() => closeTrade(t)}>Close</button>}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>

        {autoOn && (
          <div className="card">
            <div className="mb-2 font-semibold text-white">Auto-trading activity</div>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap text-xs text-slate-400">{scanLog.length ? scanLog.join('\n') : 'Waiting for the first scan…'}</pre>
          </div>
        )}

        {/* History */}
        <div className="card p-0">
          <div className="border-b border-ink-600/60 px-4 py-3 font-semibold text-white">Trade history</div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead><tr><th className="th">Closed</th><th className="th">Mode</th><th className="th">Pair</th><th className="th">Side</th><th className="th">Lots</th><th className="th">Entry</th><th className="th">Exit</th><th className="th">P&amp;L</th><th className="th">Reason</th></tr></thead>
              <tbody>
                {closed.length === 0 && <tr><td className="td text-slate-500" colSpan={9}>No closed trades yet.</td></tr>}
                {closed.map((t) => {
                  const d = decimals(t.symbol)
                  return (
                    <tr key={t.id} className="border-t border-ink-700">
                      <td className="td whitespace-nowrap">{fmtDate(t.closed_at)}</td>
                      <td className="td"><ModeBadge mode={t.mode} /></td>
                      <td className="td">{t.symbol}</td>
                      <td className="td"><DirBadge dir={t.direction} /></td>
                      <td className="td">{t.lots}</td>
                      <td className="td font-mono">{Number(t.entry_price).toFixed(d)}</td>
                      <td className="td font-mono">{t.close_price != null ? Number(t.close_price).toFixed(d) : '—'}</td>
                      <td className={`td font-mono ${Number(t.pnl_usd ?? 0) < 0 ? 'text-sell' : 'text-buy'}`}>{t.pnl_usd != null ? money(Number(t.pnl_usd)) : 'see broker'}{t.mode === 'live' && t.pnl_usd != null ? ' (est.)' : ''}</td>
                      <td className="td">{t.close_reason ?? '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>

        <Notice tone="warn">Automated trading carries risk of loss. Signals are rule-based and not guaranteed. Live profit and loss figures are estimates; your broker statement is the record.</Notice>
      </div>
    </>
  )
}

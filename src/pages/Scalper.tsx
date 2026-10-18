import { useEffect, useMemo, useRef, useState } from 'react'
import { Bell, CheckCircle2, ChevronDown, Lock, OctagonX, Pause, Play, Plug, ShieldAlert, Smartphone, WifiOff, XCircle, Zap } from 'lucide-react'
import { useAuth } from '../lib/auth'
import { ErrorBox, Notice, PageHeader, Skeleton, Toast, money } from '../components/ui'
import { Sheet, TypedConfirm } from '../components/scalper/Sheet'
import { ConfigPanel } from '../components/scalper/ConfigPanel'
import { AnalysisCard, Closed, Events, Positions, Quotes, Signals, StatsGrid } from '../components/scalper/Panels'
import {
  ageSec, buzz, computeStats, enableAlerts, notify, sendCommand, trustedMode, useScalper, useWakeLock,
  type CommandName,
} from '../lib/scalper'

type Tab = 'overview' | 'trades' | 'history' | 'settings' | 'log'
const TABS: [Tab, string][] = [['overview', 'Overview'], ['trades', 'Trades'], ['history', 'History'], ['settings', 'Settings'], ['log', 'Log']]

const MODE_STYLE: Record<string, string> = {
  'LIVE ACTIVE': 'bg-sell/25 text-red-200 ring-1 ring-sell/60',
  'LIVE ARMED': 'bg-amber-500/20 text-amber-200 ring-1 ring-amber-400/50',
  PAPER: 'bg-brand-cyan/15 text-cyan-200 ring-1 ring-brand-cyan/40',
  PAUSED: 'bg-slate-500/20 text-slate-200 ring-1 ring-white/15',
  ERROR: 'bg-sell/25 text-red-200 ring-1 ring-sell/60',
  DISCONNECTED: 'bg-slate-500/20 text-slate-300 ring-1 ring-white/10',
  'ENGINE OFFLINE': 'bg-amber-500/20 text-amber-200 ring-1 ring-amber-400/50',
  'NO ENGINE': 'bg-slate-500/20 text-slate-300 ring-1 ring-white/10',
}

type Sheets = null | 'estop' | 'close' | 'live' | 'unlock'

export default function Scalper() {
  const d = useScalper()
  const { isAdmin } = useAuth()
  const [tab, setTab] = useState<Tab>('overview')
  const [sheet, setSheet] = useState<Sheets>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [typed, setTyped] = useState('')
  const [preview, setPreview] = useState<any>(null)
  const [statMode, setStatMode] = useState<'live' | 'paper'>('paper')
  const [keepAwake, setKeepAwake] = useState(false)
  const awake = useWakeLock(keepAwake)
  const [alertsOn, setAlertsOn] = useState(typeof Notification !== 'undefined' && Notification.permission === 'granted')

  const s = d.state
  const mode = trustedMode(s, d.now)
  const hb = ageSec(s?.heartbeat_at, d.now)
  const engineUp = mode !== 'ENGINE OFFLINE' && mode !== 'NO ENGINE'
  const connected = engineUp && !!s?.mt5_connected
  const locked = !!s?.trading_locked
  const mt5Age = ageSec(s?.last_mt5_heartbeat_at, d.now)

  // Haptics and a system notification for every new critical risk event while the page is open.
  const seen = useRef<number | null>(null)
  useEffect(() => {
    const crit = d.events.filter((e) => e.severity === 'critical' && !e.resolved_at)
    const top = crit[0]?.id ?? 0
    if (seen.current !== null && top > seen.current) { buzz([200, 100, 200, 100, 400]); void notify('ForexAI Scalper: CRITICAL', crit[0].message) }
    seen.current = Math.max(seen.current ?? 0, top)
  }, [d.events])

  const stats = useMemo(() => computeStats(d.closed.filter((p) => p.mode === statMode)), [d.closed, statMode])
  const openLive = d.positions.filter((p) => p.mode === 'live')
  const floating = d.positions.reduce((a, p) => a + Number(p.floating_pnl ?? 0), 0)

  const run = async (cmd: CommandName, payload: Record<string, unknown> = {}, ok?: string, haptic = true) => {
    setBusy(cmd); setErr(null)
    try {
      const r = await sendCommand(cmd, payload)
      if (haptic) buzz(r.status === 'done' ? 40 : [80, 60, 80])
      if (r.status !== 'done') {
        const detail = r.result?.error ?? r.result?.message ?? r.status
        setErr(`${cmd.replace(/_/g, ' ')}: ${detail}`)
      } else setMsg(ok ?? r.result?.message ?? 'Done')
      void d.reload()
      return r
    } catch (e) { setErr(e instanceof Error ? e.message : 'Command failed'); return null }
    finally { setBusy(null) }
  }

  const closeSheet = () => { setSheet(null); setTyped(''); setPreview(null) }

  const openLiveSheet = async () => {
    setSheet('live'); setPreview(null); setTyped('')
    const r = await run('request_live_preview', {}, 'Live pre-check complete', false)
    if (r) setPreview(r.result)
  }

  const noPlan = !!s && !engineUp
  const canResume = connected && !locked && s?.exec_mode !== 'none' && s?.paused
  const canPause = connected && s?.exec_mode !== 'none' && !s?.paused

  if (d.loading) return <div className="space-y-3"><Skeleton className="h-16" /><Skeleton className="h-40" /><Skeleton className="h-40" /></div>

  return (
    <div className="space-y-4 pb-6">
      {/* Sticky status bar: always visible, with the emergency stop one tap away */}
      <div className="sticky top-0 z-30 -mx-4 border-b border-white/[0.08] bg-ink-900/95 px-4 py-2 backdrop-blur md:-mx-8 md:px-8">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <span className={`badge px-3 py-1 text-[12px] font-extrabold tracking-wide ${MODE_STYLE[mode]}`}>{mode}</span>
            <div className="mt-0.5 truncate text-[10px] text-slate-400">
              {engineUp ? `engine ${Math.round(hb ?? 0)}s ago · MT5 ${s?.mt5_connected ? (mt5Age != null ? `ok ${Math.round(mt5Age)}s` : 'ok') : 'disconnected'}` : s ? `last engine contact ${hb == null ? 'never' : `${Math.round(hb / 60)} min ago`}` : 'engine has never reported'}
            </div>
          </div>
          <button onClick={() => { buzz(30); setSheet('estop') }} disabled={!engineUp}
            className="flex min-h-[48px] shrink-0 items-center gap-2 rounded-xl bg-sell px-4 text-sm font-extrabold text-white shadow-lg shadow-red-900/40 active:scale-95 disabled:opacity-40">
            <OctagonX size={18} /> STOP
          </button>
        </div>
      </div>

      <PageHeader title="Scalper" subtitle="XAUUSD and BTCUSD on your MT5 account" />

      {!d.online && <Notice tone="warn"><WifiOff size={14} className="mr-1 inline" /> You are offline. What you see may be out of date. The bot and its broker stop-losses keep running on the VPS.</Notice>}
      {!s && <Notice tone="warn">The engine has not reported yet. Start it on your Windows VPS (see the README), then this page fills in by itself.</Notice>}
      {noPlan && s && <Notice tone="warn"><b>Engine offline.</b> Nothing below is live. Open trades are protected only by the stop-loss already placed at your broker. Check your VPS.</Notice>}
      {locked && engineUp && (
        <div className="rounded-2xl border border-sell/50 bg-sell/10 p-3 text-sm text-red-100">
          <div className="flex items-center gap-2 font-bold"><ShieldAlert size={16} /> Trading is locked</div>
          <div className="mt-1 break-words text-[13px]">{s?.lock_reason}</div>
          <button className="btn-ghost mt-2 w-full" onClick={() => setSheet('unlock')}><Lock size={14} /> Review and unlock</button>
        </div>
      )}
      <ErrorBox message={d.error ?? err} />

      {/* ---------- Controls ---------- */}
      <div className="card space-y-3">
        <div className="grid grid-cols-2 gap-2">
          <button className="btn-ghost" disabled={!engineUp || !!busy} onClick={() => void run('connect', {}, 'MT5 connected')}><Plug size={16} /> {s?.mt5_connected ? 'Reconnect MT5' : 'Connect MT5'}</button>
          <button className="btn-ghost" disabled={!connected || !!busy} onClick={() => void run(s?.analysis_running ? 'stop_analysis' : 'start_analysis')}>
            {s?.analysis_running ? <><Pause size={16} /> Stop analysis</> : <><Play size={16} /> Start analysis</>}
          </button>
          <button className="btn-primary" disabled={!connected || !!busy || locked} onClick={() => void run('start_paper', {}, 'Paper trading started')}><Zap size={16} /> Start paper</button>
          <button className="btn-ghost !border-amber-400/40 !text-amber-200" disabled={!connected || !!busy || locked} onClick={() => void openLiveSheet()}><ShieldAlert size={16} /> Arm live…</button>
          {canPause && <button className="btn-ghost col-span-2" disabled={!!busy} onClick={() => void run('pause_entries')}><Pause size={16} /> Pause new entries</button>}
          {canResume && <button className="btn-buy col-span-2" disabled={!!busy} onClick={() => void run('resume_entries')}><Play size={16} /> Resume {s?.exec_mode} entries</button>}
          {s?.exec_mode === 'live' && connected && <button className="btn-ghost col-span-2" disabled={!!busy} onClick={() => void run('disarm_live', {}, 'Live disarmed')}>Disarm live</button>}
        </div>
        <div className="grid grid-cols-2 gap-2">
          <button className="btn-ghost" disabled={!connected || !!busy} onClick={() => void run('reconcile')}>Reconcile with broker</button>
          <button className="btn-ghost !border-sell/40 !text-red-300" disabled={!connected || !!busy || d.positions.length === 0} onClick={() => setSheet('close')}>Close bot trades…</button>
        </div>
        {busy && <div className="text-center text-xs text-slate-400">Waiting for the engine to confirm “{busy.replace(/_/g, ' ')}”…</div>}
      </div>

      {/* ---------- Tabs ---------- */}
      <div className="sticky top-[62px] z-20 -mx-4 flex gap-1 overflow-x-auto border-b border-white/[0.06] bg-ink-950/90 px-4 py-1.5 backdrop-blur md:-mx-8 md:px-8" role="tablist">
        {TABS.map(([k, t]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
            className={`min-h-[44px] shrink-0 rounded-xl px-4 text-sm font-semibold ${tab === k ? 'bg-brand-cyan/15 text-cyan-100 ring-1 ring-brand-cyan/40' : 'text-slate-400'}`}>{t}
            {k === 'trades' && d.positions.length > 0 && <span className="ml-1.5 rounded-full bg-white/10 px-1.5 text-[10px]">{d.positions.length}</span>}
            {k === 'log' && d.events.some((e) => e.severity === 'critical' && !e.resolved_at) && <span className="ml-1.5 inline-block h-2 w-2 rounded-full bg-sell" />}
          </button>))}
      </div>

      {tab === 'overview' && s && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-2">
            {(() => {
              const live = s.account ?? {}
              const paper = live.paper
              const useAcct = s.exec_mode === 'paper' || !live.balance ? paper ?? live : live
              return [
                ['Balance', money(useAcct?.balance)], ['Equity', money(useAcct?.equity)],
                ['Free margin', money(s.exec_mode === 'paper' ? null : live.free_margin)], ['Floating P&L', money(floating)],
              ].map(([k, v]) => (
                <div key={k} className="rounded-xl border border-white/[0.07] bg-white/[0.03] p-3">
                  <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{k}</div>
                  <div className="mt-0.5 text-base font-extrabold text-white">{v}</div>
                </div>))
            })()}
          </div>
          <div className="text-[11px] text-slate-400">
            {s.exec_mode === 'paper' ? 'Showing the simulated paper account. ' : ''}
            {s.account?.login ? `MT5 ${s.account.type} account ${s.account.login} · ${s.account.server} · ${s.account.currency}` : 'MT5 account not reported'}
          </div>
          <Quotes state={s} />
          <div className="text-[11px] text-slate-400">
            Last analysis {s.last_analysis_at ? new Date(s.last_analysis_at).toLocaleTimeString() : '—'}
            {s.health?.next_eval ? ` · next candle check in ${Math.max(0, Math.round(s.health.next_eval - d.now / 1000))}s` : ''}
          </div>
          <div className="space-y-2">{Object.entries(s.analysis ?? {}).map(([i, a]) => <AnalysisCard key={i} inst={i} a={a} />)}</div>
          {openLive.length > 0 && <Notice tone="warn">{openLive.length} LIVE trade(s) open with real money. See the Trades tab.</Notice>}

          <div className="card space-y-2">
            <div className="label !mb-0">Phone helpers</div>
            <button className="btn-ghost w-full justify-between" onClick={() => setKeepAwake((v) => !v)}>
              <span className="flex items-center gap-2"><Smartphone size={16} /> Keep screen on</span><span className="text-xs">{keepAwake ? (awake ? 'ON' : 'unavailable') : 'OFF'}</span>
            </button>
            <button className="btn-ghost w-full justify-between" onClick={async () => { const r = await enableAlerts(); setAlertsOn(r === 'granted'); setMsg(r === 'granted' ? 'Alerts enabled while this app is open' : 'Notifications are blocked or unsupported on this browser') }}>
              <span className="flex items-center gap-2"><Bell size={16} /> Critical alerts on this phone</span><span className="text-xs">{alertsOn ? 'ON' : 'OFF'}</span>
            </button>
            <p className="text-[11px] text-slate-500">Alerts here only work while this page is open. For alerts when your phone is locked, set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on the VPS. The engine then messages you directly.</p>
          </div>
        </div>
      )}

      {tab === 'trades' && <Positions rows={d.positions} />}

      {tab === 'history' && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-2">
            {(['paper', 'live'] as const).map((m) => (
              <button key={m} onClick={() => setStatMode(m)} className={`min-h-[44px] rounded-xl border text-sm font-bold ${statMode === m ? 'border-brand-cyan bg-brand-cyan/10 text-cyan-100' : 'border-white/10 text-slate-300'}`}>{m === 'live' ? 'Live results' : 'Paper results'}</button>))}
          </div>
          <StatsGrid s={stats} label={statMode} />
          <div className="label">Closed trades</div>
          <Closed rows={d.closed.filter((p) => p.mode === statMode)} />
          <div className="label">Signals and rejected entries</div>
          <Signals rows={d.signals} />
        </div>
      )}

      {tab === 'settings' && (
        <ConfigPanel stored={d.config?.config ?? null} isAdmin={isAdmin} onSaved={(m) => { setMsg(m); void d.reload() }} />
      )}

      {tab === 'log' && <Events rows={d.events} audit={d.audit} />}

      {/* ---------- Sheets ---------- */}
      <Sheet open={sheet === 'estop'} title="Emergency stop" onClose={closeSheet}>
        <p className="text-sm text-slate-300">This immediately disables new entries and disarms live trading. Open trades are <b>not</b> closed: they keep their broker stop-loss and the engine keeps managing them. Closing trades is a separate action.</p>
        <div className="mt-4 grid gap-2">
          <button className="btn-sell min-h-[52px] text-base" disabled={!!busy} onClick={async () => { const r = await run('emergency_stop', {}, 'Emergency stop done. Entries disabled.'); if (r?.status === 'done') closeSheet() }}><OctagonX size={18} /> Stop all new entries now</button>
          <button className="btn-ghost" onClick={closeSheet}>Cancel</button>
        </div>
      </Sheet>

      <Sheet open={sheet === 'close'} title="Close bot trades" onClose={closeSheet}>
        <p className="text-sm text-slate-300">Closes every trade opened by this bot at market price ({d.positions.length} open). Real trades will realise their current profit or loss.</p>
        <div className="mt-3"><TypedConfirm word="CLOSE" value={typed} onChange={setTyped} /></div>
        <div className="mt-3 grid grid-cols-2 gap-2">
          {(['all', 'paper', 'live'] as const).map((m) => (
            <button key={m} className={`btn-ghost ${m === 'all' ? 'col-span-2' : ''} !border-sell/40 !text-red-200`} disabled={typed !== 'CLOSE' || !!busy}
              onClick={async () => { const r = await run('close_positions', { mode: m, confirm_text: 'CLOSE' }, `Closed ${m} trades`); if (r?.status === 'done') closeSheet() }}>Close {m === 'all' ? 'ALL bot trades' : `${m} only`}</button>))}
        </div>
      </Sheet>

      <Sheet open={sheet === 'unlock'} title="Unlock trading" onClose={closeSheet}>
        <p className="text-sm text-slate-300">Reason for the lock:</p>
        <div className="mt-1 rounded-xl bg-white/[0.05] p-3 text-sm text-red-100">{s?.lock_reason}</div>
        <p className="mt-2 text-xs text-slate-400">Only unlock after you have checked MT5 and every open trade has a stop-loss. Entries stay paused afterwards until you resume.</p>
        <div className="mt-3"><TypedConfirm word="UNLOCK" value={typed} onChange={setTyped} /></div>
        <button className="btn-primary mt-3 w-full" disabled={typed !== 'UNLOCK' || !!busy} onClick={async () => { const r = await run('unlock_trading', { confirm_text: 'UNLOCK' }); if (r?.status === 'done') closeSheet() }}>Unlock</button>
      </Sheet>

      <Sheet open={sheet === 'live'} title="Arm LIVE trading" onClose={closeSheet}>
        {!preview ? <div className="space-y-2"><Skeleton className="h-10" /><Skeleton className="h-24" /><div className="text-center text-xs text-slate-400">Running the broker pre-checks…</div></div> : (
          <div className="space-y-3">
            <div className="rounded-2xl border border-amber-400/40 bg-amber-500/10 p-3 text-[13px] text-amber-100">{preview.warning}</div>
            {preview.account && (
              <div className="rounded-xl bg-white/[0.05] p-3 text-sm">
                <div>Account <b className="text-white">{preview.account.login}</b> · {preview.account.server}</div>
                <div>Type <b className={preview.account.type?.toLowerCase().includes('real') ? 'text-red-300' : 'text-green-300'}>{preview.account.type}</b> · {preview.account.currency}</div>
                <div>Equity {money(preview.account.equity)}</div>
              </div>)}
            {preview.instruments && <div className="text-sm">Instruments: {Object.entries(preview.instruments).map(([k, v]) => `${k}→${v}`).join(', ')}</div>}
            {preview.risk && (
              <div className="grid grid-cols-2 gap-1.5 text-[12px]">
                {Object.entries(preview.risk).map(([k, v]) => <div key={k} className="rounded-lg bg-white/[0.04] p-2"><span className="text-slate-400">{k.replace(/_/g, ' ')}</span><br /><b className="text-white">{String(v)}</b></div>)}
              </div>)}
            <div className="space-y-1">
              {(preview.checks ?? []).map((c: any, i: number) => (
                <div key={i} className="flex items-start gap-2 text-[12px]">
                  {c.ok ? <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-buy-400" /> : <XCircle size={14} className={`mt-0.5 shrink-0 ${c.blocking ? 'text-sell-400' : 'text-amber-300'}`} />}
                  <span className={c.ok ? 'text-slate-300' : c.blocking ? 'text-red-200' : 'text-amber-200'}>{c.name}{c.detail ? ` — ${c.detail}` : ''}</span>
                </div>))}
            </div>
            {preview.ok && preview.preview_token ? (
              <>
                <TypedConfirm word="ARM LIVE" value={typed} onChange={setTyped} />
                <p className="text-[11px] text-slate-400">Arming does not place a trade. Entries stay paused until you tap Resume. This check expires in about 2 minutes.</p>
                <button className="btn-sell w-full" disabled={typed !== 'ARM LIVE' || !!busy}
                  onClick={async () => { const r = await run('arm_live', { preview_token: preview.preview_token, confirm_text: 'ARM LIVE' }, 'Live ARMED. Entries are paused until you resume.'); if (r?.status === 'done') closeSheet() }}>Arm live trading</button>
              </>
            ) : <Notice tone="warn">Live cannot be armed until every blocking check passes. Fix the red items, then reopen this screen. Test on a demo account first.</Notice>}
            <button className="btn-ghost w-full" onClick={closeSheet}><ChevronDown size={16} /> Cancel</button>
          </div>)}
      </Sheet>

      <Toast message={msg} />
    </div>
  )
}

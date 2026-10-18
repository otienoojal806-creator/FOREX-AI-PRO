import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { supabase } from './supabase'

export type BotMode = 'DISCONNECTED' | 'PAPER' | 'LIVE ARMED' | 'LIVE ACTIVE' | 'PAUSED' | 'ERROR'

export interface ScalperState {
  mode: BotMode
  heartbeat_at: string | null
  mt5_connected: boolean
  last_mt5_heartbeat_at: string | null
  trading_locked: boolean
  lock_reason: string | null
  analysis_running: boolean
  entries_enabled: boolean
  paused: boolean
  exec_mode: 'none' | 'paper' | 'live'
  account: any
  quotes: Record<string, { symbol: string; bid: number; ask: number; spread_points: number; age_sec: number }>
  analysis: Record<string, any>
  health: { checks?: Record<string, { ok: boolean; detail: string }>; warnings?: string[]; next_eval?: number; live_allowed_on_vps?: boolean } & Record<string, any>
  last_analysis_at: string | null
  engine_version: string | null
}

export interface ScalperPosition {
  id: string; mode: 'paper' | 'live'; ticket: number; instrument: string; broker_symbol: string; side: 'BUY' | 'SELL'
  volume: number; entry_price: number; sl: number | null; tp: number | null; original_sl: number | null
  trail_active: boolean; breakeven_done: boolean; trail_mode: string | null; current_price: number | null
  floating_pnl: number | null; status: 'open' | 'closed'; close_price: number | null; pnl: number | null; fees: number | null
  close_reason: string | null; opened_at: string; closed_at: string | null; original_risk: number | null
}

export interface ScalperSignal {
  id: string; signal_id: string; instrument: string; decision: 'BUY' | 'SELL' | 'NO TRADE'; outcome: string
  reject_reason: string | null; analysis: any; created_at: string
}
export interface RiskEvent { id: number; severity: 'info' | 'warn' | 'critical'; kind: string; message: string; resolved_at: string | null; created_at: string }
export interface AuditRow { id: number; actor: string; action: string; details: any; created_at: string }
export interface CommandRow { id: string; command: string; status: string; result: any; created_at: string }

/** The engine heartbeats every ~2s. Older than this and the dashboard must not claim anything is running. */
export const STALE_AFTER_SEC = 15

export function ageSec(iso: string | null | undefined, now = Date.now()): number | null {
  if (!iso) return null
  const t = Date.parse(iso)
  return Number.isFinite(t) ? Math.max(0, (now - t) / 1000) : null
}

/** Mode the user may trust: if the engine is silent we show OFFLINE instead of the last stored mode. */
export function trustedMode(s: ScalperState | null, now = Date.now()): BotMode | 'ENGINE OFFLINE' | 'NO ENGINE' {
  if (!s) return 'NO ENGINE'
  const a = ageSec(s.heartbeat_at, now)
  if (a === null || a > STALE_AFTER_SEC) return 'ENGINE OFFLINE'
  return s.mode
}

export interface Stats {
  trades: number; wins: number; losses: number; winRate: number | null; profitFactor: number | null
  net: number; fees: number; expectancy: number | null; avgWin: number | null; avgLoss: number | null
  maxDrawdown: number; todayPnl: number; todayTrades: number
}

/** Computed from real closed-position rows (pnl is already net of commission/swap/fees by the engine). */
export function computeStats(closed: ScalperPosition[], now = new Date()): Stats {
  const rows = closed.filter((p) => p.status === 'closed' && p.pnl != null).sort((a, b) => Date.parse(a.closed_at ?? '') - Date.parse(b.closed_at ?? ''))
  const dayStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  let gp = 0, gl = 0, wins = 0, losses = 0, net = 0, fees = 0, peak = 0, eq = 0, dd = 0, today = 0, todayN = 0
  for (const p of rows) {
    const v = Number(p.pnl)
    net += v; fees += Number(p.fees ?? 0)
    if (v > 0) { gp += v; wins++ } else if (v < 0) { gl += -v; losses++ }
    eq += v; peak = Math.max(peak, eq); dd = Math.max(dd, peak - eq)
    if (Date.parse(p.closed_at ?? '') >= dayStart) { today += v; todayN++ }
  }
  const n = rows.length
  return {
    trades: n, wins, losses, winRate: n ? wins / n : null,
    profitFactor: gl > 0 ? gp / gl : gp > 0 ? Infinity : null,
    net, fees, expectancy: n ? net / n : null, avgWin: wins ? gp / wins : null, avgLoss: losses ? -gl / losses : null,
    maxDrawdown: dd, todayPnl: today, todayTrades: todayN,
  }
}

export interface ScalperData {
  state: ScalperState | null
  positions: ScalperPosition[]
  closed: ScalperPosition[]
  signals: ScalperSignal[]
  events: RiskEvent[]
  audit: AuditRow[]
  config: { config: any; version: number } | null
  loading: boolean
  error: string | null
  online: boolean
  reload: () => Promise<void>
  now: number
}

/** Loads real backend data and keeps it live over Supabase Realtime, with polling as a fallback for flaky mobile networks. */
export function useScalper(): ScalperData {
  const [state, setState] = useState<ScalperState | null>(null)
  const [positions, setPositions] = useState<ScalperPosition[]>([])
  const [closed, setClosed] = useState<ScalperPosition[]>([])
  const [signals, setSignals] = useState<ScalperSignal[]>([])
  const [events, setEvents] = useState<RiskEvent[]>([])
  const [audit, setAudit] = useState<AuditRow[]>([])
  const [config, setConfig] = useState<{ config: any; version: number } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine)
  const [now, setNow] = useState(Date.now())
  const busy = useRef(false)

  const reload = useCallback(async () => {
    if (busy.current) return
    busy.current = true
    try {
      const [st, op, cl, sg, ev, au, cf] = await Promise.all([
        supabase.from('scalper_state').select('*').maybeSingle(),
        supabase.from('scalper_positions').select('*').eq('status', 'open').order('opened_at', { ascending: false }),
        supabase.from('scalper_positions').select('*').eq('status', 'closed').order('closed_at', { ascending: false }).limit(500),
        supabase.from('scalper_signals').select('*').order('created_at', { ascending: false }).limit(60),
        supabase.from('scalper_risk_events').select('*').order('created_at', { ascending: false }).limit(40),
        supabase.from('scalper_audit').select('id,actor,action,details,created_at').order('id', { ascending: false }).limit(60),
        supabase.from('scalper_config').select('config,version').maybeSingle(),
      ])
      const firstErr = [st, op, cl, sg, ev, au, cf].find((r) => r.error)?.error
      if (firstErr) setError(firstErr.message); else setError(null)
      setState((st.data as ScalperState) ?? null)
      setPositions((op.data as ScalperPosition[]) ?? [])
      setClosed((cl.data as ScalperPosition[]) ?? [])
      setSignals((sg.data as ScalperSignal[]) ?? [])
      setEvents((ev.data as RiskEvent[]) ?? [])
      setAudit((au.data as AuditRow[]) ?? [])
      setConfig((cf.data as any) ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load')
    } finally {
      busy.current = false
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
    let timer: number | undefined
    const kick = () => { window.clearTimeout(timer); timer = window.setTimeout(() => void reload(), 400) }
    const ch = supabase.channel('scalper-live')
    for (const table of ['scalper_state', 'scalper_positions', 'scalper_signals', 'scalper_risk_events', 'scalper_commands']) {
      ch.on('postgres_changes', { event: '*', schema: 'public', table }, kick)
    }
    ch.subscribe()
    const poll = window.setInterval(() => { if (document.visibilityState === 'visible') void reload() }, 5000)
    const tick = window.setInterval(() => setNow(Date.now()), 1000)
    const on = () => { setOnline(true); void reload() }
    const off = () => setOnline(false)
    const vis = () => { if (document.visibilityState === 'visible') void reload() }
    window.addEventListener('online', on); window.addEventListener('offline', off)
    document.addEventListener('visibilitychange', vis)
    return () => {
      window.clearTimeout(timer); window.clearInterval(poll); window.clearInterval(tick)
      window.removeEventListener('online', on); window.removeEventListener('offline', off)
      document.removeEventListener('visibilitychange', vis)
      void supabase.removeChannel(ch)
    }
  }, [reload])

  return useMemo(() => ({ state, positions, closed, signals, events, audit, config, loading, error, online, reload, now }),
    [state, positions, closed, signals, events, audit, config, loading, error, online, reload, now])
}

export type CommandName = 'connect' | 'discover_symbols' | 'start_analysis' | 'stop_analysis' | 'start_paper' | 'request_live_preview'
  | 'arm_live' | 'disarm_live' | 'pause_entries' | 'resume_entries' | 'emergency_stop' | 'close_positions' | 'unlock_trading' | 'reconcile'

/**
 * Queues a command and waits for the engine's real answer. A command is only "done" when the engine says so;
 * if the engine is offline it times out and says so instead of pretending.
 */
export async function sendCommand(command: CommandName, payload: Record<string, unknown> = {}, timeoutMs = 25000): Promise<{ status: string; result: any }> {
  const { data: id, error } = await supabase.rpc('scalper_send_command', { _command: command, _payload: payload })
  if (error) throw new Error(error.message)
  const until = Date.now() + timeoutMs
  while (Date.now() < until) {
    await new Promise((r) => setTimeout(r, 1000))
    const { data } = await supabase.from('scalper_commands').select('status,result').eq('id', id as string).maybeSingle()
    if (data && !['pending', 'processing'].includes(data.status)) return data as { status: string; result: any }
  }
  throw new Error('The engine did not answer. It may be offline or busy; check the status bar before retrying.')
}

export async function saveConfig(config: unknown): Promise<number> {
  const { data, error } = await supabase.rpc('scalper_save_config', { _config: config })
  if (error) throw new Error(error.message)
  return data as number
}

/** Phone helpers: haptics, alerts and keeping the screen awake. All best-effort. */
export const buzz = (pattern: number | number[] = 60) => { try { navigator.vibrate?.(pattern) } catch { /* unsupported */ } }

export async function enableAlerts(): Promise<NotificationPermission | 'unsupported'> {
  if (typeof Notification === 'undefined') return 'unsupported'
  try { return await Notification.requestPermission() } catch { return 'unsupported' }
}

export async function notify(title: string, body: string): Promise<void> {
  try {
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return
    const reg = await navigator.serviceWorker?.getRegistration()
    if (reg) await reg.showNotification(title, { body, tag: 'scalper-critical', icon: '/icon-192.png' })
    else new Notification(title, { body })
  } catch { /* some mobile browsers only allow service-worker notifications */ }
}

export function useWakeLock(enabled: boolean): boolean {
  const [held, setHeld] = useState(false)
  useEffect(() => {
    if (!enabled || !('wakeLock' in navigator)) { setHeld(false); return }
    let lock: WakeLockSentinel | null = null
    let cancelled = false
    const acquire = async () => {
      try { lock = await navigator.wakeLock.request('screen'); if (!cancelled) setHeld(true); lock.addEventListener('release', () => setHeld(false)) } catch { setHeld(false) }
    }
    void acquire()
    const vis = () => { if (document.visibilityState === 'visible') void acquire() }
    document.addEventListener('visibilitychange', vis)
    return () => { cancelled = true; document.removeEventListener('visibilitychange', vis); void lock?.release(); setHeld(false) }
  }, [enabled])
  return held
}

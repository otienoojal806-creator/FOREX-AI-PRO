import { useState } from 'react'
import { AlertOctagon, AlertTriangle, CheckCircle2, Info, TrendingDown, TrendingUp, XCircle } from 'lucide-react'
import { DirBadge, EmptyState, fmtDate, money } from '../ui'
import type { AuditRow, RiskEvent, ScalperPosition, ScalperSignal, ScalperState, Stats } from '../../lib/scalper'

const px = (n: number | null | undefined, d = 2) => (n == null ? '—' : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }))
const pnlCls = (n: number | null | undefined) => (n == null || n === 0 ? 'text-slate-200' : n > 0 ? 'text-buy-400' : 'text-sell-400')
const digitsFor = (inst: string) => (inst.startsWith('BTC') ? 2 : 2)

export function Quotes({ state }: { state: ScalperState }) {
  const items = Object.entries(state.quotes ?? {})
  if (!items.length) return <div className="card"><EmptyState title="No live prices yet" hint="Connect MT5 and start analysis. Prices come straight from your broker terminal." /></div>
  return (
    <div className="grid grid-cols-2 gap-3">
      {items.map(([inst, q]) => {
        const a = state.analysis?.[inst]
        const stale = q.age_sec > 20
        return (
          <div key={inst} className="card !p-3.5">
            <div className="flex items-center justify-between">
              <span className="text-xs font-bold text-slate-300">{inst}</span>
              {a && <span className={`text-[10px] font-bold ${a.decision === 'BUY' ? 'text-buy-400' : a.decision === 'SELL' ? 'text-sell-400' : 'text-slate-400'}`}>{a.decision}</span>}
            </div>
            <div className="mt-1 text-xl font-extrabold text-white">{px(q.bid, digitsFor(inst))}</div>
            <div className="text-[11px] text-slate-400">ask {px(q.ask, digitsFor(inst))} · spread {q.spread_points} pts</div>
            <div className={`mt-1 text-[10px] ${stale ? 'text-amber-300' : 'text-slate-500'}`}>{stale ? `stale ${Math.round(q.age_sec)}s (market closed?)` : 'live'}</div>
          </div>
        )
      })}
    </div>
  )
}

export function AnalysisCard({ inst, a }: { inst: string; a: any }) {
  const [open, setOpen] = useState(false)
  if (!a) return null
  const ind = a.indicators ?? {}
  return (
    <div className="card !p-3.5">
      <button className="flex w-full items-center justify-between gap-2 text-left" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-bold text-white">{inst}</span>
            {a.decision === 'NO TRADE' ? <span className="badge bg-slate-500/15 text-slate-300">NO TRADE</span> : <DirBadge dir={a.decision} />}
          </div>
          <div className="mt-0.5 truncate text-[11px] text-slate-400">
            M5 {a.trend?.m5 ?? '—'} · M1 {a.trend?.m1 ?? '—'} · RSI {a.momentum?.rsi ?? '—'}
          </div>
        </div>
        <span className="text-xs text-slate-500">{open ? 'Hide' : 'Details'}</span>
      </button>
      {open && (
        <div className="mt-3 space-y-2 text-[12px]">
          <div className="grid grid-cols-3 gap-2 text-center">
            {[['EMA9', ind.ema9], ['EMA21', ind.ema21], ['EMA50', ind.ema50], ['RSI14', ind.rsi14], ['ATR14', ind.atr14], ['Spread', a.spread_points]].map(([k, v]) => (
              <div key={String(k)} className="rounded-xl bg-white/[0.04] p-2"><div className="text-[10px] uppercase text-slate-400">{k}</div><div className="font-semibold text-white">{v == null ? '—' : String(v)}</div></div>
            ))}
          </div>
          {a.decision !== 'NO TRADE' && (
            <div className="grid grid-cols-3 gap-2 text-center">
              <div className="rounded-xl bg-sell/10 p-2"><div className="text-[10px] uppercase text-slate-400">SL</div><div className="font-semibold text-sell-400">{px(a.sl_price)}</div></div>
              <div className="rounded-xl bg-buy/10 p-2"><div className="text-[10px] uppercase text-slate-400">TP</div><div className="font-semibold text-buy-400">{px(a.tp_price)}</div></div>
              <div className="rounded-xl bg-white/[0.04] p-2"><div className="text-[10px] uppercase text-slate-400">Net R:R</div><div className="font-semibold text-white">{a.rr_net ?? '—'}</div></div>
            </div>
          )}
          {a.sizing && <div className="text-slate-300">Risk {money(a.sizing.risk_amount)} · volume {a.sizing.volume} lots</div>}
          {(a.rationale?.length ?? 0) > 0 && <ul className="list-disc space-y-0.5 pl-4 text-slate-300">{a.rationale.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>}
          {(a.reject_reasons?.length ?? 0) > 0 && <ul className="list-disc space-y-0.5 pl-4 text-amber-200">{a.reject_reasons.map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>}
          <div className="text-[10px] text-slate-500">Analysis only. It is not an order. Strategy {a.strategy_version} · bar {a.bar_time_iso ?? ''}</div>
        </div>
      )}
    </div>
  )
}

export function Positions({ rows }: { rows: ScalperPosition[] }) {
  if (!rows.length) return <div className="card"><EmptyState title="No open trades" hint="Only broker-confirmed fills appear here." /></div>
  return (
    <div className="space-y-3">
      {rows.map((p) => {
        const protectedStop = p.sl != null && p.sl > 0
        const lockedIn = p.sl != null && (p.side === 'BUY' ? p.sl >= p.entry_price : p.sl <= p.entry_price)
        return (
          <div key={p.id} className={`card !p-3.5 ${protectedStop ? '' : 'ring-1 ring-sell/60'}`}>
            <div className="flex items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-bold text-white">{p.instrument}</span><DirBadge dir={p.side} />
                <span className={`badge ${p.mode === 'live' ? 'bg-sell/20 text-red-300' : 'bg-brand-cyan/15 text-cyan-200'}`}>{p.mode === 'live' ? 'LIVE' : 'PAPER'}</span>
              </div>
              <div className={`text-lg font-extrabold ${pnlCls(p.floating_pnl)}`}>{money(p.floating_pnl)}</div>
            </div>
            <div className="mt-2 grid grid-cols-4 gap-2 text-center text-[11px]">
              {[['Volume', p.volume], ['Fill', px(p.entry_price)], ['Now', px(p.current_price)], ['Ticket', p.ticket]].map(([k, v]) => (
                <div key={String(k)} className="rounded-lg bg-white/[0.04] p-1.5"><div className="text-[9px] uppercase text-slate-400">{k}</div><div className="truncate font-semibold text-white">{String(v)}</div></div>
              ))}
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2 text-[12px]">
              <div className="rounded-lg bg-sell/10 p-2">SL <b className="text-sell-400">{px(p.sl)}</b></div>
              <div className="rounded-lg bg-buy/10 p-2">TP <b className="text-buy-400">{px(p.tp)}</b></div>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
              {!protectedStop && <span className="badge bg-sell/20 text-red-300"><AlertOctagon size={11} className="mr-1" />NO STOP LOSS</span>}
              <span className={`badge ${p.trail_active ? 'bg-buy/15 text-green-300' : 'bg-slate-500/15 text-slate-300'}`}>trail {p.trail_active ? 'active' : 'waiting'}{p.trail_mode ? ` (${p.trail_mode})` : ''}</span>
              {p.breakeven_done && <span className="badge bg-buy/15 text-green-300">break-even set</span>}
              {lockedIn && <span className="badge bg-buy/15 text-green-300">profit locked</span>}
            </div>
            <div className="mt-1 text-[10px] text-slate-500">opened {fmtDate(p.opened_at)}</div>
          </div>
        )
      })}
    </div>
  )
}

export function StatsGrid({ s, label }: { s: Stats; label: string }) {
  const pf = s.profitFactor == null ? '—' : s.profitFactor === Infinity ? '∞' : s.profitFactor.toFixed(2)
  const cells: [string, string, string?][] = [
    ['Today P&L', money(s.todayPnl), pnlCls(s.todayPnl)],
    [`${label} net`, money(s.net), pnlCls(s.net)],
    ['Win rate', s.winRate == null ? '—' : `${(s.winRate * 100).toFixed(0)}%`],
    ['Profit factor', pf],
    ['Max drawdown', money(-s.maxDrawdown), s.maxDrawdown > 0 ? 'text-sell-400' : undefined],
    ['Fees paid', money(-s.fees)],
    ['Avg win', money(s.avgWin)],
    ['Avg loss', money(s.avgLoss)],
    ['Expectancy', money(s.expectancy)],
    ['Trades', `${s.trades} (${s.todayTrades} today)`],
  ]
  return (
    <div className="grid grid-cols-2 gap-2">
      {cells.map(([k, v, c]) => (
        <div key={k} className="rounded-xl border border-white/[0.07] bg-white/[0.03] p-3">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{k}</div>
          <div className={`mt-0.5 break-words text-base font-extrabold ${c ?? 'text-white'}`}>{v}</div>
        </div>
      ))}
    </div>
  )
}

export function Signals({ rows }: { rows: ScalperSignal[] }) {
  if (!rows.length) return <div className="card"><EmptyState title="No analyses yet" /></div>
  return (
    <div className="space-y-2">
      {rows.map((r) => (
        <div key={r.id} className="rounded-xl border border-white/[0.07] bg-white/[0.03] p-3 text-sm">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2"><b className="text-white">{r.instrument}</b>{r.decision === 'NO TRADE' ? <span className="badge bg-slate-500/15 text-slate-300">NO TRADE</span> : <DirBadge dir={r.decision} />}</div>
            <span className="text-[10px] text-slate-500">{fmtDate(r.created_at)}</span>
          </div>
          <div className="mt-1 text-[12px] text-slate-300">
            {r.outcome === 'live_executed' ? 'Executed LIVE (broker confirmed)' : r.outcome === 'paper_executed' ? 'Executed in PAPER (simulated)' : r.outcome === 'analysis' ? 'Analysis only, no order' : r.outcome}
            {r.reject_reason ? ` · ${r.reject_reason}` : ''}
          </div>
        </div>
      ))}
    </div>
  )
}

export function Closed({ rows }: { rows: ScalperPosition[] }) {
  if (!rows.length) return <div className="card"><EmptyState title="No closed trades yet" /></div>
  return (
    <div className="space-y-2">
      {rows.slice(0, 60).map((p) => (
        <div key={p.id} className="flex items-center justify-between gap-2 rounded-xl border border-white/[0.07] bg-white/[0.03] p-3 text-sm">
          <div className="min-w-0">
            <div className="flex items-center gap-2"><b className="text-white">{p.instrument}</b><DirBadge dir={p.side} /><span className="text-[10px] text-slate-500">{p.mode}</span></div>
            <div className="mt-0.5 truncate text-[11px] text-slate-400">{px(p.entry_price)} → {px(p.close_price)} · {p.close_reason ?? 'closed'}</div>
            <div className="text-[10px] text-slate-500">{fmtDate(p.closed_at)}</div>
          </div>
          <div className={`flex items-center gap-1 text-base font-extrabold ${pnlCls(p.pnl)}`}>{(p.pnl ?? 0) >= 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />}{money(p.pnl)}</div>
        </div>
      ))}
    </div>
  )
}

export function Events({ rows, audit }: { rows: RiskEvent[]; audit: AuditRow[] }) {
  const Ico = { info: Info, warn: AlertTriangle, critical: XCircle } as const
  const cls = { info: 'text-cyan-200', warn: 'text-amber-300', critical: 'text-red-300' } as const
  return (
    <div className="space-y-4">
      <div>
        <div className="label">Risk events and errors</div>
        {rows.length === 0 ? <div className="card"><EmptyState title="No risk events" icon={<CheckCircle2 size={22} />} /></div> : (
          <div className="space-y-2">
            {rows.map((e) => { const I = Ico[e.severity]; return (
              <div key={e.id} className="flex items-start gap-2 rounded-xl border border-white/[0.07] bg-white/[0.03] p-3 text-[13px]">
                <I size={16} className={`mt-0.5 shrink-0 ${cls[e.severity]}`} />
                <div className="min-w-0"><div className="break-words text-slate-200">{e.message}</div>
                  <div className="text-[10px] text-slate-500">{e.kind} · {fmtDate(e.created_at)}{e.resolved_at ? ' · resolved' : ''}</div></div>
              </div>) })}
          </div>)}
      </div>
      <div>
        <div className="label">Audit trail (tamper-evident)</div>
        <div className="space-y-1.5">
          {audit.map((a) => (
            <div key={a.id} className="flex items-center justify-between gap-2 rounded-lg bg-white/[0.03] px-3 py-2 text-[12px]">
              <span className="min-w-0 truncate text-slate-200"><b>{a.action}</b> <span className="text-slate-500">· {a.actor}</span></span>
              <span className="shrink-0 text-[10px] text-slate-500">{fmtDate(a.created_at)}</span>
            </div>))}
        </div>
      </div>
    </div>
  )
}

import { useEffect, useState, type ReactNode } from 'react'
import { AlertTriangle, CheckCircle2, Clock, Inbox, XCircle } from 'lucide-react'

export function PageHeader({ title, subtitle, right }: { title: string; subtitle?: string; right?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3 animate-fade-up">
      <div className="min-w-0">
        <h1 className="text-xl font-extrabold tracking-tight text-white sm:text-2xl">{title}</h1>
        {subtitle && <p className="mt-1 break-words text-sm text-slate-400">{subtitle}</p>}
      </div>
      {right}
    </div>
  )
}

export function ErrorBox({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <div className="flex items-start gap-2 rounded-xl border border-sell/40 bg-sell/10 p-3 text-sm text-red-200 break-words">
      <AlertTriangle size={16} className="mt-0.5 shrink-0" /> <span>{message}</span>
    </div>
  )
}

export function Notice({ tone = 'info', children }: { tone?: 'info' | 'warn' | 'ok'; children: ReactNode }) {
  const c = tone === 'warn' ? 'border-amber-500/40 bg-amber-500/10 text-amber-200'
    : tone === 'ok' ? 'border-buy/40 bg-buy/10 text-green-200' : 'border-brand-cyan/30 bg-brand-cyan/10 text-cyan-100'
  return <div className={`rounded-xl border p-3 text-sm break-words ${c}`}>{children}</div>
}

const STATUS: Record<string, { cls: string; label: string; Icon: typeof Clock }> = {
  pending: { cls: 'bg-amber-500/15 text-amber-300', label: 'Pending verification', Icon: Clock },
  verified: { cls: 'bg-buy/15 text-green-300', label: 'Verified', Icon: CheckCircle2 },
  rejected: { cls: 'bg-sell/15 text-red-300', label: 'Rejected', Icon: XCircle },
  active: { cls: 'bg-buy/15 text-green-300', label: 'Active', Icon: CheckCircle2 },
  revoked: { cls: 'bg-sell/15 text-red-300', label: 'Revoked', Icon: XCircle },
  none: { cls: 'bg-slate-500/15 text-slate-300', label: 'Not purchased', Icon: Clock },
}
export function StatusBadge({ status }: { status: string }) {
  const s = STATUS[status] ?? STATUS.none
  return <span className={`badge gap-1 ${s.cls}`}><s.Icon size={12} />{s.label}</span>
}

export function DirBadge({ dir }: { dir: 'BUY' | 'SELL' }) {
  return <span className={`badge px-3 font-bold tracking-wide ${dir === 'BUY' ? 'bg-buy/20 text-buy-400' : 'bg-sell/20 text-sell-400'}`}>{dir}</span>
}

export const fmtDate = (s: string | null) => (s ? new Date(s).toLocaleString() : '—')
export const money = (n: number | null | undefined) =>
  n == null ? '—' : `${n < 0 ? '-' : ''}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`


export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />
}

export function EmptyState({ title, hint, icon }: { title: string; hint?: string; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 px-4 py-8 text-center">
      <div className="grid h-12 w-12 place-items-center rounded-2xl bg-white/[0.05] text-slate-400">{icon ?? <Inbox size={22} />}</div>
      <div className="text-sm font-semibold text-slate-200">{title}</div>
      {hint && <div className="max-w-xs text-xs text-slate-400">{hint}</div>}
    </div>
  )
}

// Transient success/error toast. Pass the same message state the page already holds.
export function Toast({ message, tone = 'ok' }: { message: string | null; tone?: 'ok' | 'err' }) {
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    if (!message) { setVisible(false); return }
    setVisible(true)
    const t = window.setTimeout(() => setVisible(false), 4500)
    return () => window.clearTimeout(t)
  }, [message])
  if (!message || !visible) return null
  const Icon = tone === 'ok' ? CheckCircle2 : AlertTriangle
  return (
    <div role="status" aria-live="polite" className="pointer-events-none fixed inset-x-0 bottom-24 z-50 flex justify-center px-4 md:bottom-6">
      <button type="button" onClick={() => setVisible(false)}
        className={`pointer-events-auto flex max-w-md animate-toast-in items-start gap-2 rounded-2xl border px-4 py-3 text-left text-sm font-medium shadow-2xl backdrop-blur ${tone === 'ok' ? 'border-buy/50 bg-ink-900/95 text-green-100' : 'border-sell/50 bg-ink-900/95 text-red-100'}`}>
        <Icon size={18} className={`mt-0.5 shrink-0 ${tone === 'ok' ? 'text-buy-400' : 'text-sell-400'}`} />
        <span className="break-words">{message}</span>
      </button>
    </div>
  )
}

export function Stat({ label, children, tone }: { label: string; children: ReactNode; tone?: 'buy' | 'sell' }) {
  return (
    <div className="card !p-3.5 sm:!p-4">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">{label}</div>
      <div className={`mt-1 break-words text-lg font-extrabold sm:text-xl ${tone === 'buy' ? 'text-buy-400' : tone === 'sell' ? 'text-sell-400' : 'text-white'}`}>{children}</div>
    </div>
  )
}

// Circular confidence gauge. Shows the real signal confidence (0-100).
export function ConfidenceRing({ value, dir, size = 56 }: { value: number; dir: 'BUY' | 'SELL'; size?: number }) {
  const r = 20, c = 2 * Math.PI * r
  const v = Math.max(0, Math.min(100, value))
  const color = dir === 'BUY' ? '#16c784' : '#ea3943'
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }} role="img" aria-label={`Confidence ${v}%`}>
      <svg viewBox="0 0 48 48" width={size} height={size} className="-rotate-90">
        <circle cx="24" cy="24" r={r} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="4" />
        <circle cx="24" cy="24" r={r} fill="none" stroke={color} strokeWidth="4" strokeLinecap="round" strokeDasharray={c} strokeDashoffset={c * (1 - v / 100)} />
      </svg>
      <div className="absolute inset-0 grid place-items-center text-[13px] font-bold text-white">{v}%</div>
    </div>
  )
}

// Compact signal card used for signal history and dashboard.
export function SignalCard({ symbol, interval, dir, confidence, entry, tp, sl, time }: {
  symbol: string; interval?: string; dir: 'BUY' | 'SELL'; confidence: number; entry: string; tp: string; sl: string; time?: string
}) {
  return (
    <div className="rounded-2xl border border-white/[0.07] bg-white/[0.03] p-3.5">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-bold text-white">{symbol}</span><DirBadge dir={dir} />
            {interval && <span className="text-xs text-slate-400">{interval}</span>}
          </div>
          {time && <div className="mt-0.5 text-[11px] text-slate-500">{time}</div>}
        </div>
        <ConfidenceRing value={confidence} dir={dir} size={48} />
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-center">
        <div className="rounded-xl bg-white/[0.04] p-2"><div className="text-[10px] uppercase tracking-wider text-slate-400">Entry</div><div className="mt-0.5 break-all text-[13px] font-semibold text-white">{entry}</div></div>
        <div className="rounded-xl bg-buy/10 p-2"><div className="text-[10px] uppercase tracking-wider text-slate-400">TP</div><div className="mt-0.5 break-all text-[13px] font-semibold text-buy-400">{tp}</div></div>
        <div className="rounded-xl bg-sell/10 p-2"><div className="text-[10px] uppercase tracking-wider text-slate-400">SL</div><div className="mt-0.5 break-all text-[13px] font-semibold text-sell-400">{sl}</div></div>
      </div>
    </div>
  )
}

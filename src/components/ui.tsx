import type { ReactNode } from 'react'
import { AlertTriangle, CheckCircle2, Clock, XCircle } from 'lucide-react'

export function PageHeader({ title, subtitle, right }: { title: string; subtitle?: string; right?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-bold text-white">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}
      </div>
      {right}
    </div>
  )
}

export function ErrorBox({ message }: { message: string | null }) {
  if (!message) return null
  return (
    <div className="flex items-start gap-2 rounded-lg border border-sell/40 bg-sell/10 p-3 text-sm text-red-300">
      <AlertTriangle size={16} className="mt-0.5 shrink-0" /> <span>{message}</span>
    </div>
  )
}

export function Notice({ tone = 'info', children }: { tone?: 'info' | 'warn' | 'ok'; children: ReactNode }) {
  const c = tone === 'warn' ? 'border-amber-500/40 bg-amber-500/10 text-amber-200'
    : tone === 'ok' ? 'border-buy/40 bg-buy/10 text-green-200' : 'border-accent/40 bg-accent/10 text-blue-200'
  return <div className={`rounded-lg border p-3 text-sm ${c}`}>{children}</div>
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
  return <span className={`badge ${dir === 'BUY' ? 'bg-buy/15 text-buy' : 'bg-sell/15 text-sell'}`}>{dir}</span>
}

export const fmtDate = (s: string | null) => (s ? new Date(s).toLocaleString() : '—')
export const money = (n: number | null | undefined) =>
  n == null ? '—' : `${n < 0 ? '-' : ''}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Activity, ArrowRight, Bot, Lock } from 'lucide-react'
import { supabase } from '../lib/supabase'
import { useAuth } from '../lib/auth'
import { px } from '../lib/format'
import { MODULES, type ModuleKey, type SignalRow } from '../lib/types'
import { EmptyState, PageHeader, SignalCard, Skeleton, Stat, StatusBadge, fmtDate, money } from '../components/ui'

const LINKS: Record<ModuleKey, { to: string; Icon: typeof Activity; grad: string }> = {
  ai_analysis: { to: '/analysis', Icon: Activity, grad: 'from-cyan-500/20 via-blue-600/10' },
  auto_trading: { to: '/automation', Icon: Bot, grad: 'from-indigo-500/25 via-blue-700/10' },
}

interface TradeLite { status: 'pending' | 'open' | 'closed'; pnl_usd: number | null; mode: 'simulated' | 'live' }

export default function Dashboard() {
  const { user, access, hasModule } = useAuth()
  const [signals, setSignals] = useState<SignalRow[] | null>(null)
  const [trades, setTrades] = useState<TradeLite[] | null>(null)
  const hasAI = hasModule('ai_analysis')
  const hasAuto = hasModule('auto_trading')

  useEffect(() => {
    if (!hasAI) { setSignals([]); return }
    supabase.from('signals').select('*').order('created_at', { ascending: false }).limit(4)
      .then(({ data }) => setSignals((data ?? []) as SignalRow[]))
  }, [hasAI])
  useEffect(() => {
    if (!hasAuto) { setTrades([]); return }
    supabase.from('trades').select('status,pnl_usd,mode').limit(500)
      .then(({ data }) => setTrades((data ?? []) as TradeLite[]))
  }, [hasAuto])

  const closed = (trades ?? []).filter((t) => t.status === 'closed')
  const wins = closed.filter((t) => Number(t.pnl_usd ?? 0) > 0).length
  const net = closed.reduce((a, t) => a + Number(t.pnl_usd ?? 0), 0)

  return (
    <>
      <PageHeader title="Dashboard" subtitle={`Signed in as ${user?.email}`} />

      <div className="grid gap-4 md:grid-cols-2">
        {(Object.keys(MODULES) as ModuleKey[]).map((m) => {
          const { to, Icon, grad } = LINKS[m]
          const on = hasModule(m)
          return (
            <div key={m} className={`card flex flex-col gap-3 bg-gradient-to-br ${grad} to-transparent ${on ? 'card-glow' : ''}`}>
              <div className="flex items-start justify-between gap-3">
                <div className="flex min-w-0 items-center gap-3">
                  <div className="grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-white/[0.08] text-cyan-200"><Icon size={22} /></div>
                  <div className="min-w-0 text-lg font-bold leading-tight text-white">{MODULES[m].title}</div>
                </div>
                <StatusBadge status={access[m]?.status ?? 'none'} />
              </div>
              <p className="text-sm text-slate-300">{MODULES[m].blurb}</p>
              {on ? (
                <Link to={to} className="btn-primary self-start">Open <ArrowRight size={16} /></Link>
              ) : (
                <Link to="/billing" className="btn-ghost self-start"><Lock size={14} /> Activate for $200</Link>
              )}
            </div>
          )
        })}
      </div>

      {hasAuto && (
        <section className="mt-6">
          <h2 className="mb-3 text-base font-bold text-white">Trading performance</h2>
          {trades === null ? (
            <div className="grid grid-cols-3 gap-3"><Skeleton className="h-20" /><Skeleton className="h-20" /><Skeleton className="h-20" /></div>
          ) : (
            <div className="grid grid-cols-3 gap-3">
              <Stat label="Total trades">{closed.length}</Stat>
              <Stat label="Win rate">{closed.length ? `${Math.round((wins / closed.length) * 100)}%` : '—'}</Stat>
              <Stat label="Net P&L" tone={closed.length ? (net >= 0 ? 'buy' : 'sell') : undefined}>{closed.length ? money(net) : '—'}</Stat>
            </div>
          )}
          <p className="mt-2 text-[11px] text-slate-500">Closed trades only. Simulated and live results are combined; live figures are estimates.</p>
        </section>
      )}

      {hasAI && (
        <section className="mt-6">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-base font-bold text-white">Latest saved signals</h2>
            <Link to="/analysis" className="flex min-h-[40px] items-center gap-1 text-sm font-semibold text-brand-cyan">View all <ArrowRight size={14} /></Link>
          </div>
          {signals === null ? (
            <div className="grid gap-3 md:grid-cols-2"><Skeleton className="h-32" /><Skeleton className="h-32" /></div>
          ) : signals.length === 0 ? (
            <div className="card"><EmptyState title="No saved signals yet" hint="Open AI Analysis, analyse a market and tap Save signal." /></div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {signals.map((s) => (
                <SignalCard key={s.id} symbol={s.symbol} interval={s.interval} dir={s.direction} confidence={s.confidence}
                  entry={px(s.symbol, s.entry)} tp={px(s.symbol, s.tp)} sl={px(s.symbol, s.sl)} time={fmtDate(s.created_at)} />
              ))}
            </div>
          )}
        </section>
      )}
    </>
  )
}

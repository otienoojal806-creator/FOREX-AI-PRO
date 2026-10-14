import { Link } from 'react-router-dom'
import { Activity, Bot, Lock } from 'lucide-react'
import { useAuth } from '../lib/auth'
import { MODULES, type ModuleKey } from '../lib/types'
import { PageHeader, StatusBadge } from '../components/ui'

const LINKS: Record<ModuleKey, { to: string; Icon: typeof Activity }> = {
  ai_analysis: { to: '/analysis', Icon: Activity },
  auto_trading: { to: '/automation', Icon: Bot },
}

export default function Dashboard() {
  const { user, access, hasModule } = useAuth()
  return (
    <>
      <PageHeader title="Dashboard" subtitle={`Signed in as ${user?.email}`} />
      <div className="grid gap-5 md:grid-cols-2">
        {(Object.keys(MODULES) as ModuleKey[]).map((m) => {
          const { to, Icon } = LINKS[m]
          const on = hasModule(m)
          return (
            <div key={m} className="card flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-lg font-semibold text-white"><Icon size={20} className="text-accent-soft" />{MODULES[m].title}</div>
                <StatusBadge status={access[m]?.status ?? 'none'} />
              </div>
              <p className="text-sm text-slate-400">{MODULES[m].blurb}</p>
              {on ? (
                <Link to={to} className="btn-primary self-start">Open</Link>
              ) : (
                <Link to="/billing" className="btn-ghost self-start"><Lock size={14} /> Activate for $200</Link>
              )}
            </div>
          )
        })}
      </div>
    </>
  )
}

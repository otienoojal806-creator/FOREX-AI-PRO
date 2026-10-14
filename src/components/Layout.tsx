import { NavLink, Outlet } from 'react-router-dom'
import { Activity, Bot, CreditCard, LayoutDashboard, Lock, LogOut, Receipt, Settings, Shield, Sparkles } from 'lucide-react'
import { useAuth } from '../lib/auth'

function Item({ to, icon: Icon, label, locked }: { to: string; icon: typeof Activity; label: string; locked?: boolean }) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      className={({ isActive }) =>
        `flex items-center gap-3 whitespace-nowrap rounded-lg px-3 py-2 text-sm font-medium transition ${
          isActive ? 'bg-accent/15 text-accent-soft' : 'text-slate-400 hover:bg-ink-700 hover:text-slate-200'
        }`
      }
    >
      <Icon size={18} />
      <span>{label}</span>
      {locked && <Lock size={13} className="ml-auto text-slate-500" />}
    </NavLink>
  )
}

export default function Layout() {
  const { user, isAdmin, hasModule, signOut } = useAuth()
  return (
    <div className="flex h-full flex-col md:flex-row">
      <aside className="shrink-0 border-b border-ink-600/60 bg-ink-900 md:w-64 md:border-b-0 md:border-r">
        <div className="flex items-center gap-2 px-4 py-4">
          <div className="grid h-9 w-9 place-items-center rounded-xl bg-accent/20 text-accent-soft"><Sparkles size={18} /></div>
          <div>
            <div className="text-sm font-bold leading-none text-white">ForexAI Pro</div>
            <div className="mt-1 text-[11px] text-slate-500">AI trading workspace</div>
          </div>
        </div>
        <nav className="flex gap-1 overflow-x-auto px-3 pb-3 md:flex-col md:overflow-visible">
          <Item to="/" icon={LayoutDashboard} label="Dashboard" />
          <Item to="/analysis" icon={Activity} label="AI Analysis" locked={!hasModule('ai_analysis')} />
          <Item to="/automation" icon={Bot} label="Trade Automation" locked={!hasModule('auto_trading')} />
          <Item to="/billing" icon={CreditCard} label="Modules & Billing" />
          <Item to="/transactions" icon={Receipt} label="Transactions" />
          <Item to="/settings" icon={Settings} label="Account" />
          {isAdmin && <Item to="/admin" icon={Shield} label="Admin" />}
        </nav>
        <div className="hidden px-4 pb-4 md:absolute md:bottom-0 md:block md:w-64">
          <div className="truncate text-xs text-slate-500">{user?.email}</div>
          <button onClick={() => void signOut()} className="mt-2 flex items-center gap-2 text-sm text-slate-400 hover:text-white">
            <LogOut size={16} /> Sign out
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl p-4 md:p-8">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

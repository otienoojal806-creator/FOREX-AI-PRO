import { useEffect, useRef, useState, type RefObject } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { Activity, Bot, CreditCard, LayoutDashboard, Lock, LogOut, Receipt, Settings, Shield, Sparkles, User, Wallet, X } from 'lucide-react'
import { useAuth } from '../lib/auth'

// Presentation only: labels each table cell with its column header so tables can stack as cards on phones.
function useResponsiveTables(root: RefObject<HTMLElement>) {
  const { pathname } = useLocation()
  useEffect(() => {
    const el = root.current
    if (!el) return
    const apply = () => {
      el.querySelectorAll('table').forEach((t) => {
        t.classList.add('rt')
        const heads = Array.from(t.querySelectorAll('thead th')).map((h) => h.textContent?.trim() ?? '')
        t.querySelectorAll('tbody tr').forEach((tr) => {
          Array.from(tr.children).forEach((td, i) => {
            if (!td.hasAttribute('data-label')) td.setAttribute('data-label', heads[i] ?? '')
          })
        })
      })
    }
    apply()
    const mo = new MutationObserver(apply)
    mo.observe(el, { childList: true, subtree: true })
    return () => mo.disconnect()
  }, [root, pathname])
}

function Item({ to, icon: Icon, label, locked }: { to: string; icon: typeof Activity; label: string; locked?: boolean }) {
  return (
    <NavLink to={to} end={to === '/'}
      className={({ isActive }) =>
        `flex min-h-[44px] items-center gap-3 whitespace-nowrap rounded-xl px-3 py-2 text-sm font-semibold transition ${
          isActive ? 'bg-brand-cyan/10 text-cyan-200 ring-1 ring-brand-cyan/30' : 'text-slate-400 hover:bg-white/[0.05] hover:text-slate-100'
        }`}>
      <Icon size={18} />
      <span>{label}</span>
      {locked && <Lock size={13} className="ml-auto text-slate-500" />}
    </NavLink>
  )
}

function Tab({ to, icon: Icon, label, locked }: { to: string; icon: typeof Activity; label: string; locked?: boolean }) {
  return (
    <NavLink to={to} end={to === '/'} className="min-w-0 flex-1">
      {({ isActive }) => (
        <span className={`relative flex min-h-[56px] flex-col items-center justify-center gap-0.5 rounded-2xl px-1 text-[11px] font-semibold transition ${isActive ? 'bg-brand-cyan/10 text-cyan-300' : 'text-slate-400'}`}>
          <Icon size={21} strokeWidth={isActive ? 2.4 : 1.9} />
          <span className="max-w-full truncate">{label}</span>
          {locked && <Lock size={9} className="absolute right-2 top-1.5 text-slate-500" />}
          {isActive && <span className="absolute -top-px h-0.5 w-8 rounded-full bg-brand-cyan" />}
        </span>
      )}
    </NavLink>
  )
}

const Logo = () => (
  <div className="flex items-center gap-2.5">
    <div className="grid h-9 w-9 place-items-center rounded-xl text-white shadow-lg shadow-cyan-500/20" style={{ background: 'linear-gradient(135deg,#2563eb,#22d3ee)' }}><Sparkles size={18} /></div>
    <div>
      <div className="text-[15px] font-extrabold leading-none tracking-tight text-white">Forex<span className="text-brand-cyan">AI</span> Pro</div>
      <div className="mt-1 text-[10px] text-slate-400">AI Powered Trading System</div>
    </div>
  </div>
)

export default function Layout() {
  const { user, isAdmin, hasModule, signOut } = useAuth()
  const { pathname } = useLocation()
  const [menu, setMenu] = useState(false)
  const mainRef = useRef<HTMLElement>(null)
  useResponsiveTables(mainRef)
  useEffect(() => { setMenu(false); mainRef.current?.scrollTo({ top: 0 }) }, [pathname])
  const initial = (user?.email ?? '?').slice(0, 1).toUpperCase()

  return (
    <div className="flex h-full flex-col md:flex-row">
      {/* Desktop sidebar */}
      <aside className="hidden shrink-0 flex-col border-r border-white/[0.07] bg-ink-900/80 backdrop-blur md:flex md:w-64">
        <div className="px-4 py-5"><Logo /></div>
        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-3 pb-3">
          <Item to="/" icon={LayoutDashboard} label="Dashboard" />
          <Item to="/analysis" icon={Activity} label="AI Analysis" locked={!hasModule('ai_analysis')} />
          <Item to="/automation" icon={Bot} label="Trade Automation" locked={!hasModule('auto_trading')} />
          <Item to="/billing" icon={CreditCard} label="Modules & Billing" />
          <Item to="/transactions" icon={Receipt} label="Transactions" />
          <Item to="/settings" icon={Settings} label="Account" />
          {isAdmin && <Item to="/admin" icon={Shield} label="Admin" />}
        </nav>
        <div className="border-t border-white/[0.07] px-4 py-4">
          <div className="truncate text-xs text-slate-400">{user?.email}</div>
          <button onClick={() => void signOut()} className="mt-2 flex min-h-[40px] items-center gap-2 text-sm text-slate-300 hover:text-white">
            <LogOut size={16} /> Sign out
          </button>
        </div>
      </aside>

      {/* Mobile header */}
      <header className="relative z-30 flex shrink-0 items-center justify-between border-b border-white/[0.07] bg-ink-900/85 px-4 py-2.5 backdrop-blur md:hidden" style={{ paddingTop: 'max(10px, env(safe-area-inset-top))' }}>
        <Logo />
        <button onClick={() => setMenu((v) => !v)} aria-label="Account menu" aria-expanded={menu}
          className="grid h-11 w-11 place-items-center rounded-full border border-brand-cyan/40 bg-brand-cyan/10 text-sm font-bold text-cyan-200">
          {menu ? <X size={18} /> : initial}
        </button>
        {menu && (
          <div className="absolute right-3 top-full mt-2 w-64 animate-fade-up rounded-2xl border border-white/10 bg-ink-800 p-2 shadow-2xl">
            <div className="flex items-center gap-2 px-3 py-2 text-xs text-slate-400"><User size={14} /><span className="truncate">{user?.email}</span></div>
            <Item to="/transactions" icon={Receipt} label="Transactions" />
            {isAdmin && <Item to="/admin" icon={Shield} label="Admin" />}
            <button onClick={() => void signOut()} className="flex min-h-[44px] w-full items-center gap-3 rounded-xl px-3 py-2 text-sm font-semibold text-red-300 hover:bg-white/[0.05]">
              <LogOut size={18} /> Sign out
            </button>
          </div>
        )}
      </header>

      <main ref={mainRef} className="min-w-0 flex-1 overflow-y-auto overflow-x-hidden">
        <div className="mx-auto max-w-6xl p-4 pb-8 md:p-8">
          <div key={pathname} className="animate-fade-up"><Outlet /></div>
        </div>
      </main>

      {/* Mobile bottom navigation */}
      <nav aria-label="Primary" className="z-30 flex shrink-0 items-stretch gap-1 border-t border-white/[0.08] bg-ink-900/95 px-2 pt-1.5 backdrop-blur md:hidden" style={{ paddingBottom: 'max(8px, env(safe-area-inset-bottom))' }}>
        <Tab to="/" icon={LayoutDashboard} label="Home" />
        <Tab to="/analysis" icon={Activity} label="AI Analysis" locked={!hasModule('ai_analysis')} />
        <Tab to="/automation" icon={Bot} label="Automation" locked={!hasModule('auto_trading')} />
        <Tab to="/billing" icon={Wallet} label="Wallet" />
        <Tab to="/settings" icon={User} label="Profile" />
      </nav>
    </div>
  )
}

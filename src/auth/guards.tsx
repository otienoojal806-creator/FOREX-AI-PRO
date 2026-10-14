import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from './AuthProvider'
import type { ModuleKey } from '../lib/types'

function Splash() {
  return <div className="flex h-full items-center justify-center text-slate-400">Loading…</div>
}

export function RequireAuth() {
  const { loading, user } = useAuth()
  const loc = useLocation()
  if (loading) return <Splash />
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname }} />
  return <Outlet />
}

export function RequireAdmin() {
  const { loading, isAdmin } = useAuth()
  if (loading) return <Splash />
  if (!isAdmin) return <Navigate to="/" replace />
  return <Outlet />
}

/** UI gate only. Data access is enforced again on the server (RLS / edge functions). */
export function RequireModule({ module }: { module: ModuleKey }) {
  const { loading, hasModule } = useAuth()
  if (loading) return <Splash />
  if (!hasModule(module)) return <Navigate to="/billing" replace />
  return <Outlet />
}

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Session, User } from '@supabase/supabase-js'
import { supabase } from '../lib/supabase'
import type { ModuleAccess, ModuleKey } from '../lib/types'

interface AuthState {
  loading: boolean
  session: Session | null
  user: User | null
  isAdmin: boolean
  access: Partial<Record<ModuleKey, ModuleAccess>>
  hasModule: (m: ModuleKey) => boolean
  refresh: () => Promise<void>
  signOut: () => Promise<void>
}

const Ctx = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true)
  const [session, setSession] = useState<Session | null>(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [access, setAccess] = useState<Partial<Record<ModuleKey, ModuleAccess>>>({})

  const load = useCallback(async (s: Session | null) => {
    if (!s) {
      setIsAdmin(false)
      setAccess({})
      return
    }
    const [roles, acc] = await Promise.all([
      supabase.from('user_roles').select('role').eq('user_id', s.user.id),
      supabase.from('module_access').select('*').eq('user_id', s.user.id),
    ])
    setIsAdmin((roles.data ?? []).some((r) => r.role === 'admin'))
    const map: Partial<Record<ModuleKey, ModuleAccess>> = {}
    for (const row of (acc.data ?? []) as ModuleAccess[]) map[row.module] = row
    setAccess(map)
  }, [])

  useEffect(() => {
    supabase.auth.getSession().then(async ({ data }) => {
      setSession(data.session)
      await load(data.session)
      setLoading(false)
    })
    const { data: sub } = supabase.auth.onAuthStateChange((_evt, s) => {
      setSession(s)
      // Defer to avoid calling Supabase inside the auth callback.
      setTimeout(() => void load(s), 0)
    })
    return () => sub.subscription.unsubscribe()
  }, [load])

  const value = useMemo<AuthState>(
    () => ({
      loading,
      session,
      user: session?.user ?? null,
      isAdmin,
      access,
      hasModule: (m) => access[m]?.status === 'active',
      refresh: () => load(session),
      signOut: async () => {
        await supabase.auth.signOut()
      },
    }),
    [loading, session, isAdmin, access, load],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useAuth() {
  const v = useContext(Ctx)
  if (!v) throw new Error('useAuth must be used inside AuthProvider')
  return v
}

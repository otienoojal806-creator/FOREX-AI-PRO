import { useCallback, useEffect, useState } from 'react'
import { supabase } from '../../lib/supabase'
import { useAuth } from '../../lib/auth'
import { MODULES, type Access, type ModuleKey, type Profile } from '../../lib/types'
import { ErrorBox, StatusBadge, fmtDate } from '../../components/ui'

interface UserRow extends Profile { admin: boolean; access: Partial<Record<ModuleKey, Access>> }

export default function UsersTab() {
  const { user } = useAuth()
  const [rows, setRows] = useState<UserRow[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [q, setQ] = useState('')

  const load = useCallback(async () => {
    const [p, r, a] = await Promise.all([
      supabase.from('profiles').select('*').order('created_at', { ascending: false }).limit(1000),
      supabase.from('user_roles').select('user_id, role'),
      supabase.from('module_access').select('*'),
    ])
    if (p.error) return setErr(p.error.message)
    const admins = new Set((r.data ?? []).filter((x) => x.role === 'admin').map((x) => x.user_id as string))
    const acc = new Map<string, Partial<Record<ModuleKey, Access>>>()
    for (const x of (a.data ?? []) as Access[]) acc.set(x.user_id, { ...(acc.get(x.user_id) ?? {}), [x.module]: x })
    setRows((p.data as Profile[]).map((u) => ({ ...u, admin: admins.has(u.id), access: acc.get(u.id) ?? {} })))
  }, [])
  useEffect(() => { void load() }, [load])

  async function setAccess(u: UserRow, module: ModuleKey, active: boolean) {
    setErr(null)
    let reason: string | null = null
    if (!active) {
      reason = window.prompt(`Reason for revoking ${MODULES[module].title} for ${u.email}?`)
      if (reason === null) return
    } else if (!window.confirm(`Activate ${MODULES[module].title} for ${u.email} without a payment submission? This is a manual grant and is audit-logged.`)) return
    const { error } = await supabase.rpc('admin_set_module_access', { _user: u.id, _module: module, _active: active, _reason: reason })
    if (error) setErr(error.message); else void load()
  }
  async function setAdmin(u: UserRow, grant: boolean) {
    if (!window.confirm(`${grant ? 'Grant' : 'Remove'} administrator role for ${u.email}?`)) return
    const { error } = await supabase.rpc('admin_set_role', { _user: u.id, _role: 'admin', _grant: grant })
    if (error) setErr(error.message); else void load()
  }

  const shown = (rows ?? []).filter((u) => (u.email + (u.full_name ?? '')).toLowerCase().includes(q.toLowerCase()))
  return (
    <div className="space-y-4">
      <input className="input max-w-sm" placeholder="Search users…" value={q} onChange={(e) => setQ(e.target.value)} />
      <ErrorBox message={err} />
      <div className="card overflow-x-auto p-0">
        <table className="w-full">
          <thead className="border-b border-ink-600"><tr>
            <th className="th">User</th><th className="th">Joined</th><th className="th">AI Analysis</th><th className="th">Automated Trading</th><th className="th">Role</th>
          </tr></thead>
          <tbody>
            {shown.map((u) => (
              <tr key={u.id} className="border-b border-ink-700/60 align-top">
                <td className="td"><div className="font-medium text-white">{u.email}</div><div className="text-xs text-slate-500">{u.full_name}</div></td>
                <td className="td whitespace-nowrap">{fmtDate(u.created_at)}</td>
                {(['ai_analysis', 'auto_trading'] as ModuleKey[]).map((m) => {
                  const a = u.access[m]
                  return (
                    <td className="td" key={m}>
                      <StatusBadge status={a?.status ?? 'none'} />
                      <div className="mt-2">
                        {a?.status === 'active'
                          ? <button className="text-xs text-red-300 underline" onClick={() => void setAccess(u, m, false)}>Revoke</button>
                          : <button className="text-xs text-green-300 underline" onClick={() => void setAccess(u, m, true)}>{a ? 'Re-activate' : 'Grant'}</button>}
                      </div>
                    </td>
                  )
                })}
                <td className="td">
                  {u.admin ? 'Admin' : 'User'}
                  {u.id !== user?.id && (
                    <div className="mt-2"><button className="text-xs text-slate-300 underline" onClick={() => void setAdmin(u, !u.admin)}>{u.admin ? 'Remove admin' : 'Make admin'}</button></div>
                  )}
                </td>
              </tr>
            ))}
            {rows && shown.length === 0 && <tr><td className="td text-slate-400" colSpan={5}>No users found.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

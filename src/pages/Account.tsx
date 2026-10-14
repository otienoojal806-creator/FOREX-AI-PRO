import { useEffect, useState, type FormEvent } from 'react'
import { supabase } from '../lib/supabase'
import { useAuth } from '../lib/auth'
import { ErrorBox, Notice, PageHeader, StatusBadge } from '../components/ui'
import { MODULES, type ModuleKey } from '../lib/types'

export default function Account() {
  const { user, access, isAdmin } = useAuth()
  const [name, setName] = useState('')
  const [pw, setPw] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!user) return
    supabase.from('profiles').select('full_name').eq('id', user.id).maybeSingle().then(({ data }) => setName(data?.full_name ?? ''))
  }, [user])

  async function saveProfile(e: FormEvent) {
    e.preventDefault(); setErr(null); setMsg(null)
    const { error } = await supabase.from('profiles').update({ full_name: name.trim() }).eq('id', user!.id)
    if (error) setErr(error.message); else setMsg('Profile saved.')
  }
  async function changePw(e: FormEvent) {
    e.preventDefault(); setErr(null); setMsg(null)
    if (pw.length < 8) return setErr('Password must be at least 8 characters.')
    const { error } = await supabase.auth.updateUser({ password: pw })
    if (error) setErr(error.message); else { setMsg('Password updated.'); setPw('') }
  }

  return (
    <>
      <PageHeader title="Account" subtitle={user?.email ?? ''} />
      <div className="grid gap-5 lg:grid-cols-2">
        <div className="space-y-5">
          <form onSubmit={saveProfile} className="card space-y-3">
            <h3 className="font-semibold text-white">Profile</h3>
            <div><label className="label">Full name</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} /></div>
            <button className="btn-primary">Save</button>
          </form>
          <form onSubmit={changePw} className="card space-y-3">
            <h3 className="font-semibold text-white">Change password</h3>
            <div><label className="label">New password</label><input className="input" type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" /></div>
            <button className="btn-primary">Update password</button>
          </form>
          {msg && <Notice tone="ok">{msg}</Notice>}
          <ErrorBox message={err} />
        </div>
        <div className="card space-y-3">
          <h3 className="font-semibold text-white">Module status</h3>
          {(Object.keys(MODULES) as ModuleKey[]).map((m) => (
            <div key={m} className="flex items-center justify-between rounded-lg border border-ink-600 p-3">
              <span className="text-sm">{MODULES[m].title}</span>
              <StatusBadge status={access[m]?.status ?? 'none'} />
            </div>
          ))}
          <div className="text-xs text-slate-500">Role: {isAdmin ? 'Administrator' : 'User'}</div>
        </div>
      </div>
    </>
  )
}

import { useState, type FormEvent } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { Sparkles } from 'lucide-react'
import { supabase } from '../lib/supabase'
import { useAuth } from '../lib/auth'
import { ErrorBox, Notice } from '../components/ui'

export default function Login() {
  const { session, loading } = useAuth()
  const loc = useLocation() as { state?: { from?: string } }
  const [mode, setMode] = useState<'in' | 'up'>('in')
  const [email, setEmail] = useState('')
  const [pw, setPw] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  if (!loading && session) return <Navigate to={loc.state?.from ?? '/'} replace />

  async function submit(e: FormEvent) {
    e.preventDefault(); setErr(null); setMsg(null); setBusy(true)
    try {
      if (mode === 'in') {
        const { error } = await supabase.auth.signInWithPassword({ email, password: pw })
        if (error) throw error
      } else {
        if (pw.length < 8) throw new Error('Password must be at least 8 characters.')
        const { data, error } = await supabase.auth.signUp({ email, password: pw, options: { data: { full_name: name.trim() } } })
        if (error) throw error
        if (!data.session) setMsg('Account created. Check your email to confirm your address, then sign in.')
      }
    } catch (x) {
      setErr(x instanceof Error ? x.message : 'Something went wrong')
    } finally { setBusy(false) }
  }

  return (
    <div className="grid min-h-full place-items-center overflow-y-auto p-4">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center justify-center gap-3">
          <div className="grid h-12 w-12 place-items-center rounded-2xl text-white shadow-lg shadow-cyan-500/30" style={{ background: 'linear-gradient(135deg,#2563eb,#22d3ee)' }}><Sparkles /></div>
          <div><div className="text-2xl font-extrabold tracking-tight text-white">Forex<span className="text-brand-cyan">AI</span> Pro</div><div className="text-[11px] text-slate-400">AI Powered Trading System</div></div>
        </div>
        <form onSubmit={submit} className="card card-glow space-y-4">
          <div className="flex rounded-xl bg-ink-900/80 p-1 text-sm font-semibold">
            {(['in', 'up'] as const).map((m) => (
              <button type="button" key={m} onClick={() => { setMode(m); setErr(null); setMsg(null) }}
                className={`flex-1 min-h-[42px] rounded-lg py-2 transition ${mode === m ? 'bg-gradient-to-r from-blue-600 to-cyan-500 text-white' : 'text-slate-400'}`}>
                {m === 'in' ? 'Sign in' : 'Create account'}
              </button>
            ))}
          </div>
          {mode === 'up' && <div><label className="label">Full name</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} /></div>}
          <div><label className="label">Email</label><input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" /></div>
          <div><label className="label">Password</label><input className="input" type="password" value={pw} onChange={(e) => setPw(e.target.value)} required autoComplete={mode === 'in' ? 'current-password' : 'new-password'} /></div>
          <ErrorBox message={err} />
          {msg && <Notice tone="ok">{msg}</Notice>}
          <button className="btn-primary w-full" disabled={busy}>{busy ? 'Please wait…' : mode === 'in' ? 'Sign in' : 'Create account'}</button>
        </form>
        <p className="mt-4 text-center text-xs text-slate-500">Trading involves risk. Signals are not financial advice.</p>
      </div>
    </div>
  )
}

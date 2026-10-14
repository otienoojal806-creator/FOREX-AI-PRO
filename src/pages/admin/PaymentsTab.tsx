import { useCallback, useEffect, useState } from 'react'
import { supabase } from '../../lib/supabase'
import { MODULES, type Profile, type Submission } from '../../lib/types'
import { ErrorBox, Notice, StatusBadge, fmtDate, money } from '../../components/ui'

type Row = Submission & { email: string }

function ReviewRow({ r, onDone }: { r: Row; onDone: () => void }) {
  const [amount, setAmount] = useState('200')
  const [note, setNote] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function openProof() {
    if (!r.proof_path) return
    const { data, error } = await supabase.storage.from('payment-proofs').createSignedUrl(r.proof_path, 120)
    if (error || !data) return setErr(error?.message ?? 'Could not open proof')
    window.open(data.signedUrl, '_blank', 'noopener,noreferrer')
  }
  async function review(approve: boolean) {
    setErr(null)
    if (approve && !confirmed) return setErr('Confirm that you verified this transaction on Binance first.')
    setBusy(true)
    const { error } = await supabase.rpc('admin_review_payment', {
      _submission: r.id, _approve: approve, _verified_amount: approve ? Number(amount) : null, _note: note.trim() || null,
    })
    setBusy(false)
    if (error) setErr(error.message); else onDone()
  }

  return (
    <div className="card space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="font-semibold text-white">{MODULES[r.module].title}</div>
          <div className="text-sm text-slate-400">{r.email} · {fmtDate(r.created_at)}</div>
        </div>
        <StatusBadge status={r.status} />
      </div>
      <div className="grid gap-2 text-sm md:grid-cols-2">
        <div><span className="label">Transaction ID</span><span className="font-mono break-all">{r.tx_id}</span></div>
        <div><span className="label">User note</span>{r.note ?? '—'}</div>
      </div>
      {r.proof_path && <button className="btn-ghost" onClick={() => void openProof()}>View payment proof</button>}
      <div className="grid gap-3 md:grid-cols-2">
        <div><label className="label">Amount actually received on Binance (USD)</label>
          <input className="input" type="number" min="0" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} /></div>
        <div><label className="label">Admin note</label><input className="input" value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} /></div>
      </div>
      <label className="flex items-start gap-2 text-sm text-slate-300">
        <input type="checkbox" className="mt-1" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
        I looked up this transaction ID in the Binance account and confirmed the payment was received for the amount above.
      </label>
      <ErrorBox message={err} />
      <div className="flex gap-2">
        <button className="btn-buy" disabled={busy || !confirmed} onClick={() => void review(true)}>Verify & activate</button>
        <button className="btn-sell" disabled={busy} onClick={() => void review(false)}>Reject</button>
      </div>
    </div>
  )
}

export default function PaymentsTab({ mode }: { mode: 'pending' | 'all' }) {
  const [rows, setRows] = useState<Row[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    let q = supabase.from('payment_submissions').select('*').order('created_at', { ascending: false }).limit(500)
    if (mode === 'pending') q = q.eq('status', 'pending')
    const [s, p] = await Promise.all([q, supabase.from('profiles').select('id,email')])
    if (s.error) return setErr(s.error.message)
    const emails = new Map(((p.data ?? []) as Pick<Profile, 'id' | 'email'>[]).map((x) => [x.id, x.email]))
    setRows((s.data as Submission[]).map((r) => ({ ...r, email: emails.get(r.user_id) ?? r.user_id })))
  }, [mode])
  useEffect(() => { void load() }, [load])

  if (err) return <ErrorBox message={err} />
  if (!rows) return <div className="text-slate-400">Loading…</div>

  if (mode === 'pending') {
    return (
      <div className="space-y-4">
        <Notice>Never approve from a screenshot or transaction ID alone. Check the payment in the Binance account first.</Notice>
        {rows.length === 0 && <div className="card text-slate-400">No payments waiting for verification.</div>}
        {rows.map((r) => <ReviewRow key={r.id} r={r} onDone={() => void load()} />)}
      </div>
    )
  }
  return (
    <div className="card overflow-x-auto p-0">
      <table className="w-full">
        <thead className="border-b border-ink-600"><tr>
          <th className="th">Date</th><th className="th">User</th><th className="th">Module</th><th className="th">Transaction ID</th><th className="th">Status</th><th className="th">Amount</th>
        </tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-ink-700/60">
              <td className="td whitespace-nowrap">{fmtDate(r.created_at)}</td>
              <td className="td">{r.email}</td>
              <td className="td">{MODULES[r.module].title}</td>
              <td className="td font-mono text-xs">{r.tx_id}</td>
              <td className="td"><StatusBadge status={r.status} /></td>
              <td className="td">{money(r.verified_amount_usd)}</td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td className="td text-slate-400" colSpan={6}>No transactions.</td></tr>}
        </tbody>
      </table>
    </div>
  )
}

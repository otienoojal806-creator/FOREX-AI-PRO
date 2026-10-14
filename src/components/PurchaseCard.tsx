import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { ExternalLink, ShieldCheck, Upload } from 'lucide-react'
import { supabase } from '../lib/supabase'
import { useAuth } from '../lib/auth'
import { MODULES, type ModuleKey, type PaymentSetting, type Submission } from '../lib/types'
import { ErrorBox, Notice, StatusBadge, fmtDate } from './ui'

const MAX_PROOF = 5 * 1024 * 1024

export default function PurchaseCard({ module }: { module: ModuleKey }) {
  const { user, access, refresh } = useAuth()
  const [price, setPrice] = useState(200)
  const [latest, setLatest] = useState<Submission | null>(null)
  const [open, setOpen] = useState(false)
  const [tx, setTx] = useState('')
  const [note, setNote] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!user) return
    const [s, sub] = await Promise.all([
      supabase.from('payment_settings').select('price_usd').eq('module', module).maybeSingle(),
      supabase.from('payment_submissions').select('*').eq('user_id', user.id).eq('module', module).order('created_at', { ascending: false }).limit(1),
    ])
    if (s.data) setPrice(Number(s.data.price_usd))
    setLatest(((sub.data ?? [])[0] as Submission | undefined) ?? null)
  }, [user, module])

  useEffect(() => { void load() }, [load])

  const acc = access[module]
  const status: string = acc?.status === 'active' ? 'active'
    : acc?.status === 'revoked' ? 'revoked'
    : latest?.status === 'pending' ? 'pending'
    : latest?.status === 'rejected' ? 'rejected' : 'none'

  // The URL is read from the server at click time, so an admin change applies immediately.
  async function activate() {
    setErr(null); setMsg(null)
    const { data, error } = await supabase.from('payment_settings').select('binance_url, price_usd').eq('module', module).single()
    if (error) return setErr(error.message)
    const row = data as Pick<PaymentSetting, 'binance_url' | 'price_usd'>
    setPrice(Number(row.price_usd))
    if (!row.binance_url) return setErr('The payment link for this module has not been configured yet. Please contact the administrator.')
    window.open(row.binance_url, '_blank', 'noopener,noreferrer')
    setOpen(true)
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!user) return
    setErr(null); setMsg(null)
    const txId = tx.trim()
    if (txId.length < 6) return setErr('Enter the full Binance transaction ID.')
    if (file && file.size > MAX_PROOF) return setErr('Proof file must be 5 MB or smaller.')
    if (file && !/^(image\/|application\/pdf)/.test(file.type)) return setErr('Proof must be an image or PDF.')
    setBusy(true)
    try {
      let proof_path: string | null = null
      if (file) {
        const safe = file.name.replace(/[^A-Za-z0-9._-]/g, '_').slice(-60)
        proof_path = `${user.id}/${module}-${Date.now()}-${safe}`
        const up = await supabase.storage.from('payment-proofs').upload(proof_path, file, { contentType: file.type })
        if (up.error) throw new Error(`Proof upload failed: ${up.error.message}`)
      }
      const { error } = await supabase.from('payment_submissions').insert({
        user_id: user.id, module, tx_id: txId, proof_path, note: note.trim() || null,
      })
      if (error) {
        if (error.code === '23505') {
          throw new Error(error.message.includes('payment_one_pending')
            ? 'You already have a payment awaiting verification for this module.'
            : 'This transaction ID has already been submitted.')
        }
        throw new Error(error.message)
      }
      setMsg('Submitted. An administrator will verify the payment on Binance before activating the module.')
      setOpen(false); setTx(''); setNote(''); setFile(null)
      await load(); await refresh()
    } catch (x) {
      setErr(x instanceof Error ? x.message : 'Submission failed')
    } finally { setBusy(false) }
  }

  const m = MODULES[module]
  return (
    <div className="card flex flex-col gap-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold text-white">{m.title}</h3>
          <p className="mt-1 text-sm text-slate-400">{m.blurb}</p>
        </div>
        <StatusBadge status={status} />
      </div>

      <div className="flex items-baseline gap-2">
        <span className="text-3xl font-bold text-white">${price.toFixed(0)}</span>
        <span className="text-sm text-slate-400">one-time payment · permanent access once verified</span>
      </div>

      {status === 'active' && <Notice tone="ok">Activated on {fmtDate(acc!.activated_at)}. This module stays active unless an administrator revokes it.</Notice>}
      {status === 'revoked' && <Notice tone="warn">Access was revoked{acc?.revoke_reason ? `: ${acc.revoke_reason}` : ''}. Contact the administrator.</Notice>}
      {status === 'pending' && latest && (
        <Notice tone="warn">
          Payment submitted {fmtDate(latest.created_at)} (ID <span className="font-mono">{latest.tx_id}</span>) and waiting for admin verification. Nothing is activated until the payment is confirmed on Binance.
        </Notice>
      )}
      {status === 'rejected' && latest && (
        <Notice tone="warn">Your last submission was rejected{latest.admin_note ? `: ${latest.admin_note}` : ''}. You can pay and submit again.</Notice>
      )}
      {msg && <Notice tone="ok">{msg}</Notice>}
      <ErrorBox message={err} />

      {(status === 'none' || status === 'rejected') && !open && (
        <button className="btn-primary w-full" onClick={() => void activate()}>
          ACTIVATE <ExternalLink size={16} />
        </button>
      )}
      {(status === 'none' || status === 'rejected') && !open && (
        <button className="text-left text-xs text-slate-400 underline" onClick={() => setOpen(true)}>
          Already paid? Submit your transaction ID
        </button>
      )}

      {open && (
        <form onSubmit={submit} className="space-y-3 rounded-xl border border-ink-600 bg-ink-900/60 p-4">
          <div className="flex items-center gap-2 text-sm font-semibold text-white"><ShieldCheck size={16} className="text-accent-soft" /> Submit payment details</div>
          <p className="text-xs text-slate-400">Pay exactly ${price.toFixed(0)} on Binance, then enter the transaction ID. An administrator checks it against Binance records; a screenshot alone never activates a module.</p>
          <div>
            <label className="label">Binance transaction ID</label>
            <input className="input font-mono" value={tx} onChange={(e) => setTx(e.target.value)} required minLength={6} maxLength={128} placeholder="e.g. 4821937465…" />
          </div>
          <div>
            <label className="label">Payment proof (image or PDF, optional, max 5 MB)</label>
            <label className="btn-ghost w-full cursor-pointer">
              <Upload size={16} /> {file ? file.name : 'Choose file'}
              <input type="file" accept="image/*,application/pdf" className="hidden" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            </label>
          </div>
          <div>
            <label className="label">Note (optional)</label>
            <input className="input" value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} />
          </div>
          <div className="flex gap-2">
            <button className="btn-primary flex-1" disabled={busy}>{busy ? 'Submitting…' : 'Submit for verification'}</button>
            <button type="button" className="btn-ghost" onClick={() => setOpen(false)}>Cancel</button>
          </div>
        </form>
      )}
    </div>
  )
}

import { useEffect, useState } from 'react'
import { supabase } from '../../lib/supabase'
import { MODULES, type ModuleKey, type PaymentSetting } from '../../lib/types'
import { ErrorBox, Notice, fmtDate } from '../../components/ui'

function Row({ s, onSaved }: { s: PaymentSetting; onSaved: () => void }) {
  const [url, setUrl] = useState(s.binance_url)
  const [price, setPrice] = useState(String(s.price_usd))
  const [err, setErr] = useState<string | null>(null)
  const [ok, setOk] = useState(false)
  const [busy, setBusy] = useState(false)

  async function save() {
    setErr(null); setOk(false)
    const u = url.trim()
    if (u && !/^https:\/\//i.test(u)) return setErr('The payment URL must start with https://')
    const p = Number(price)
    if (!(p > 0)) return setErr('Price must be greater than zero.')
    setBusy(true)
    const { error } = await supabase.rpc('admin_update_payment_settings', { _module: s.module, _url: u, _price: p })
    setBusy(false)
    if (error) setErr(error.message); else { setOk(true); onSaved() }
  }

  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-white">{MODULES[s.module as ModuleKey].title}</h3>
        <span className="text-xs text-slate-500">Updated {fmtDate(s.updated_at)}</span>
      </div>
      <div><label className="label">Binance payment URL</label>
        <input className="input font-mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" /></div>
      <div className="max-w-[160px]"><label className="label">Price (USD)</label>
        <input className="input" type="number" min="1" step="1" value={price} onChange={(e) => setPrice(e.target.value)} /></div>
      <ErrorBox message={err} />
      {ok && <Notice tone="ok">Saved. The ACTIVATE button now uses this link.</Notice>}
      <button className="btn-primary" disabled={busy} onClick={() => void save()}>{busy ? 'Saving…' : 'Save'}</button>
    </div>
  )
}

export default function SettingsTab() {
  const [rows, setRows] = useState<PaymentSetting[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const load = () => supabase.from('payment_settings').select('*').order('module')
    .then(({ data, error }) => { if (error) setErr(error.message); else setRows(data as PaymentSetting[]) })
  useEffect(() => { void load() }, [])

  return (
    <div className="space-y-4">
      <Notice>The two links are independent. Changes are stored in the database and read each time a user presses ACTIVATE, so they apply immediately. Only administrators can write them.</Notice>
      <ErrorBox message={err} />
      <div className="grid gap-5 lg:grid-cols-2">
        {rows?.map((s) => <Row key={s.module + s.updated_at} s={s} onSaved={() => void load()} />)}
      </div>
    </div>
  )
}

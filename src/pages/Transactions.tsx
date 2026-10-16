import { useEffect, useState } from 'react'
import { supabase } from '../lib/supabase'
import { useAuth } from '../lib/auth'
import { MODULES, type Submission } from '../lib/types'
import { EmptyState, ErrorBox, PageHeader, Skeleton, StatusBadge, fmtDate, money } from '../components/ui'

export default function Transactions() {
  const { user } = useAuth()
  const [rows, setRows] = useState<Submission[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!user) return
    supabase.from('payment_submissions').select('*').eq('user_id', user.id).order('created_at', { ascending: false })
      .then(({ data, error }) => { if (error) setErr(error.message); else setRows(data as Submission[]) })
  }, [user])

  return (
    <>
      <PageHeader title="Transaction history" subtitle="Your payment submissions and their verification status." />
      <ErrorBox message={err} />
      <div className="card overflow-x-auto p-0">
        <table className="w-full">
          <thead className="border-b border-ink-600"><tr>
            <th className="th">Date</th><th className="th">Module</th><th className="th">Transaction ID</th><th className="th">Status</th><th className="th">Verified amount</th><th className="th">Admin note</th>
          </tr></thead>
          <tbody>
            {rows?.map((r) => (
              <tr key={r.id} className="border-b border-ink-700/60">
                <td className="td whitespace-nowrap">{fmtDate(r.created_at)}</td>
                <td className="td">{MODULES[r.module].title}</td>
                <td className="td font-mono text-xs">{r.tx_id}</td>
                <td className="td"><StatusBadge status={r.status} /></td>
                <td className="td">{money(r.verified_amount_usd)}</td>
                <td className="td text-slate-400">{r.admin_note ?? '—'}</td>
              </tr>
            ))}
            {rows && rows.length === 0 && <tr><td className="td" colSpan={6}><EmptyState title="No payments submitted yet" hint="Payments you submit from Wallet appear here with their verification status." /></td></tr>}
            {!rows && !err && <tr><td className="td" colSpan={6}><Skeleton className="h-10 w-full" /></td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}

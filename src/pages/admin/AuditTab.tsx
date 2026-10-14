import { useEffect, useState } from 'react'
import { supabase } from '../../lib/supabase'
import { ErrorBox, fmtDate } from '../../components/ui'

interface Log { id: number; actor_id: string | null; action: string; entity: string; entity_id: string | null; details: unknown; created_at: string }

export default function AuditTab() {
  const [rows, setRows] = useState<Log[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    supabase.from('audit_logs').select('*').order('created_at', { ascending: false }).limit(300)
      .then(({ data, error }) => { if (error) setErr(error.message); else setRows(data as Log[]) })
  }, [])
  return (
    <div className="space-y-3">
      <ErrorBox message={err} />
      <div className="card overflow-x-auto p-0">
        <table className="w-full">
          <thead className="border-b border-ink-600"><tr><th className="th">Time</th><th className="th">Action</th><th className="th">Entity</th><th className="th">Details</th></tr></thead>
          <tbody>
            {rows?.map((l) => (
              <tr key={l.id} className="border-b border-ink-700/60 align-top">
                <td className="td whitespace-nowrap">{fmtDate(l.created_at)}</td>
                <td className="td font-mono text-xs">{l.action}</td>
                <td className="td text-xs text-slate-400">{l.entity}<br />{l.entity_id}</td>
                <td className="td font-mono text-xs text-slate-400 break-all">{JSON.stringify(l.details)}</td>
              </tr>
            ))}
            {rows && rows.length === 0 && <tr><td className="td text-slate-400" colSpan={4}>No audit entries yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

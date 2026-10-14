import { useState } from 'react'
import { PageHeader } from '../../components/ui'
import PaymentsTab from './PaymentsTab'
import UsersTab from './UsersTab'
import SettingsTab from './SettingsTab'
import AuditTab from './AuditTab'

const TABS = [
  { id: 'payments', label: 'Payment verification' },
  { id: 'users', label: 'Users & access' },
  { id: 'settings', label: 'Payment URLs & settings' },
  { id: 'transactions', label: 'All transactions' },
  { id: 'audit', label: 'Audit log' },
] as const
type Tab = (typeof TABS)[number]['id']

export default function Admin() {
  const [tab, setTab] = useState<Tab>('payments')
  return (
    <>
      <PageHeader title="Admin dashboard" subtitle="Verify payments, manage access and configure payment links. Every action is written to the audit log." />
      <div className="mb-5 flex gap-1 overflow-x-auto rounded-xl bg-ink-900 p-1">
        {TABS.map((t) => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={`whitespace-nowrap rounded-lg px-4 py-2 text-sm font-semibold ${tab === t.id ? 'bg-accent text-white' : 'text-slate-400 hover:text-white'}`}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === 'payments' && <PaymentsTab mode="pending" />}
      {tab === 'transactions' && <PaymentsTab mode="all" />}
      {tab === 'users' && <UsersTab />}
      {tab === 'settings' && <SettingsTab />}
      {tab === 'audit' && <AuditTab />}
    </>
  )
}

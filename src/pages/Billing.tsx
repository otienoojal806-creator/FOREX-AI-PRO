import PurchaseCard from '../components/PurchaseCard'
import { PageHeader } from '../components/ui'

export default function Billing() {
  return (
    <>
      <PageHeader title="Modules & Billing" subtitle="Two independent $200 one-time purchases. Each is verified by an administrator before it activates." />
      <div className="grid gap-5 lg:grid-cols-2">
        <PurchaseCard module="ai_analysis" />
        <PurchaseCard module="auto_trading" />
      </div>
    </>
  )
}

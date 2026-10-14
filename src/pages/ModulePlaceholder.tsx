import { PageHeader, Notice } from '../components/ui'
import { MODULES, type ModuleKey } from '../lib/types'

// Phase 1 ships purchase, verification and access control. The trading screens
// (charts, signals, automation) are built on top of this gate in the next phase.
export default function ModulePlaceholder({ module }: { module: ModuleKey }) {
  const m = MODULES[module]
  return (
    <>
      <PageHeader title={m.title} subtitle={m.blurb} />
      <Notice tone="ok">Your {m.title} module is active. The full workspace is being built next.</Notice>
      <ul className="mt-4 list-inside list-disc space-y-1 text-sm text-slate-400">
        {m.features.map((f) => <li key={f}>{f}</li>)}
      </ul>
    </>
  )
}

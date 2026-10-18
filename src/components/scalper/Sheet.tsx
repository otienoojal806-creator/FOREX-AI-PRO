import { useEffect, type ReactNode } from 'react'
import { X } from 'lucide-react'

/** Bottom sheet: confirmations sit under the thumb on a phone. */
export function Sheet({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    if (!open) return
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', k)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.removeEventListener('keydown', k); document.body.style.overflow = prev }
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-[60] flex items-end justify-center sm:items-center" role="dialog" aria-modal="true" aria-label={title}>
      <button aria-label="Close" className="absolute inset-0 bg-black/70" onClick={onClose} />
      <div className="relative z-10 max-h-[88dvh] w-full max-w-lg animate-fade-up overflow-y-auto rounded-t-3xl border border-white/10 bg-ink-800 p-5 shadow-2xl sm:rounded-3xl"
        style={{ paddingBottom: 'max(20px, env(safe-area-inset-bottom))' }}>
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-lg font-extrabold text-white">{title}</h2>
          <button onClick={onClose} aria-label="Close" className="grid h-10 w-10 place-items-center rounded-full bg-white/[0.06] text-slate-300"><X size={18} /></button>
        </div>
        {children}
      </div>
    </div>
  )
}

/** Typed confirmation: the database function rejects the command unless the exact word was typed. */
export function TypedConfirm({ word, value, onChange }: { word: string; value: string; onChange: (v: string) => void }) {
  return (
    <div>
      <label className="label">Type {word} to confirm</label>
      <input className="input text-center font-bold tracking-widest" value={value} onChange={(e) => onChange(e.target.value)}
        autoCapitalize="characters" autoCorrect="off" autoComplete="off" spellCheck={false} placeholder={word} />
    </div>
  )
}

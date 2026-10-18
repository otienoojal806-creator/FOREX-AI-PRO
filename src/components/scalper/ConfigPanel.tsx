import { useEffect, useState } from 'react'
import { Minus, Plus, Save } from 'lucide-react'
import { ErrorBox, Notice } from '../ui'
import { saveConfig } from '../../lib/scalper'

type Field = { path: string[]; label: string; step: number; min: number; max: number; unit?: string; hint?: string }

// Ranges mirror the database guard in scalper_save_config; the engine also clamps to the VPS hard caps.
const RISK: Field[] = [
  { path: ['risk', 'risk_per_trade_pct'], label: 'Risk per trade', step: 0.05, min: 0.01, max: 2, unit: '% equity', hint: 'Default 0.25 for testing' },
  { path: ['risk', 'max_daily_loss_pct'], label: 'Max daily loss', step: 0.25, min: 0.1, max: 10, unit: '% equity' },
  { path: ['risk', 'max_aggregate_open_risk_pct'], label: 'Max open risk (all trades)', step: 0.25, min: 0.01, max: 5, unit: '% equity' },
  { path: ['risk', 'max_total_positions'], label: 'Max open trades', step: 1, min: 1, max: 10 },
  { path: ['risk', 'max_positions_per_symbol'], label: 'Max trades per symbol', step: 1, min: 1, max: 5 },
  { path: ['risk', 'max_consecutive_losses'], label: 'Pause after losing streak', step: 1, min: 1, max: 20 },
  { path: ['risk', 'max_margin_utilization_pct'], label: 'Max margin used', step: 5, min: 1, max: 80, unit: '%' },
  { path: ['risk', 'min_net_rr'], label: 'Min reward/risk after costs', step: 0.1, min: 0.5, max: 10 },
  { path: ['risk', 'cooldown_after_loss_sec'], label: 'Cooldown after a loss', step: 60, min: 0, max: 3600, unit: 'sec' },
  { path: ['risk', 'max_trades_per_hour'], label: 'Max trades per hour', step: 1, min: 1, max: 60 },
]

const get = (o: any, p: string[]) => p.reduce((a, k) => (a == null ? a : a[k]), o)
const setIn = (o: any, p: string[], v: any): any => {
  const c = structuredClone(o)
  let t = c
  p.slice(0, -1).forEach((k) => { t[k] = t[k] ?? {}; t = t[k] })
  t[p[p.length - 1]] = v
  return c
}
const round = (n: number, step: number) => Number((Math.round(n / step) * step).toFixed(4))

function Stepper({ f, value, onChange }: { f: Field; value: number; onChange: (v: number) => void }) {
  const clamp = (n: number) => Math.min(f.max, Math.max(f.min, round(n, f.step)))
  return (
    <div className="flex items-center justify-between gap-3 rounded-xl border border-white/[0.07] bg-white/[0.03] p-3">
      <div className="min-w-0">
        <div className="text-[13px] font-semibold text-slate-100">{f.label}</div>
        {f.hint && <div className="text-[10px] text-slate-500">{f.hint}</div>}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <button type="button" aria-label={`Decrease ${f.label}`} onClick={() => onChange(clamp(value - f.step))} className="grid h-11 w-11 place-items-center rounded-xl bg-white/[0.07] active:scale-95"><Minus size={16} /></button>
        <div className="w-[74px] text-center"><div className="text-base font-extrabold text-white">{value}</div>{f.unit && <div className="text-[9px] text-slate-400">{f.unit}</div>}</div>
        <button type="button" aria-label={`Increase ${f.label}`} onClick={() => onChange(clamp(value + f.step))} className="grid h-11 w-11 place-items-center rounded-xl bg-white/[0.07] active:scale-95"><Plus size={16} /></button>
      </div>
    </div>
  )
}

export function ConfigPanel({ stored, isAdmin, onSaved, disabledReason }: { stored: any; isAdmin: boolean; onSaved: (msg: string) => void; disabledReason?: string }) {
  const [cfg, setCfg] = useState<any>(stored)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => { setCfg(stored) }, [stored])
  if (!cfg) return <Notice tone="info">The engine creates your default settings the first time it connects. Start it on the VPS, then come back.</Notice>

  const dirty = JSON.stringify(cfg) !== JSON.stringify(stored)
  const insts: string[] = Object.keys(cfg.instruments ?? {})

  const save = async () => {
    setBusy(true); setErr(null)
    try { const v = await saveConfig(cfg); onSaved(`Settings saved (version ${v}). The engine applies them on its next cycle.`) }
    catch (e) { setErr(e instanceof Error ? e.message : 'Save failed') }
    finally { setBusy(false) }
  }

  return (
    <div className="space-y-4">
      <Notice tone="info">Changes apply to new entries only. The engine also enforces hard limits set on your VPS, which this page can never raise.</Notice>
      <div className="space-y-2">{RISK.map((f) => (
        <Stepper key={f.path.join('.')} f={f} value={Number(get(cfg, f.path) ?? f.min)} onChange={(v) => setCfg(setIn(cfg, f.path, v))} />
      ))}</div>

      <div>
        <label className="label">When a risk limit is hit</label>
        <div className="grid grid-cols-2 gap-2">
          {[['pause', 'Pause new entries', 'Keep managing open trades'], ['close_all', 'Close everything', 'Flatten all bot trades']].map(([v, t, d]) => (
            <button key={v} type="button" onClick={() => setCfg(setIn(cfg, ['risk', 'breach_policy'], v))}
              className={`rounded-xl border p-3 text-left ${get(cfg, ['risk', 'breach_policy']) === v ? 'border-brand-cyan bg-brand-cyan/10' : 'border-white/10 bg-white/[0.03]'}`}>
              <div className="text-sm font-bold text-white">{t}</div><div className="text-[10px] text-slate-400">{d}</div>
            </button>))}
        </div>
      </div>

      {insts.map((i) => {
        const ic = cfg.instruments[i]
        return (
          <div key={i} className="space-y-2 rounded-2xl border border-white/[0.08] p-3">
            <div className="flex items-center justify-between">
              <b className="text-white">{i}</b>
              <button type="button" onClick={() => setCfg(setIn(cfg, ['instruments', i, 'enabled'], !ic.enabled))}
                className={`min-h-[40px] rounded-full px-4 text-xs font-bold ${ic.enabled ? 'bg-buy/20 text-green-300' : 'bg-slate-500/20 text-slate-300'}`}>{ic.enabled ? 'Enabled' : 'Disabled'}</button>
            </div>
            <div>
              <label className="label">Broker symbol {isAdmin ? '' : '(admin only)'}</label>
              <input className="input" value={ic.broker_symbol ?? ''} disabled={!isAdmin} autoCapitalize="characters" autoCorrect="off" spellCheck={false}
                onChange={(e) => setCfg(setIn(cfg, ['instruments', i, 'broker_symbol'], e.target.value.trim()))} />
            </div>
            <Stepper f={{ path: [], label: 'Max spread', step: i.startsWith('BTC') ? 250 : 5, min: 1, max: 100000, unit: 'points' }}
              value={Number(ic.max_spread_points)} onChange={(v) => setCfg(setIn(cfg, ['instruments', i, 'max_spread_points'], v))} />
            <Stepper f={{ path: [], label: 'Max lots', step: i.startsWith('BTC') ? 0.05 : 0.05, min: 0.01, max: 100, unit: 'lots' }}
              value={Number(ic.max_lots)} onChange={(v) => setCfg(setIn(cfg, ['instruments', i, 'max_lots'], v))} />
            <div>
              <label className="label">Trailing stop</label>
              <div className="grid grid-cols-2 gap-2">
                {[['atr', 'ATR based'], ['fixed', 'Fixed distance']].map(([v, t]) => (
                  <button key={v} type="button" onClick={() => setCfg(setIn(cfg, ['instruments', i, 'trail', 'mode'], v))}
                    className={`min-h-[44px] rounded-xl border text-sm font-semibold ${ic.trail?.mode === v ? 'border-brand-cyan bg-brand-cyan/10 text-cyan-100' : 'border-white/10 bg-white/[0.03] text-slate-300'}`}>{t}</button>))}
              </div>
            </div>
            <Stepper f={{ path: [], label: 'Trail starts after', step: 0.1, min: 0.1, max: 10, unit: '× ATR' }}
              value={Number(ic.trail?.activation_atr ?? 1)} onChange={(v) => setCfg(setIn(cfg, ['instruments', i, 'trail', 'activation_atr'], v))} />
            <Stepper f={{ path: [], label: 'Trail distance', step: 0.1, min: 0.1, max: 10, unit: '× ATR' }}
              value={Number(ic.trail?.distance_atr ?? 1)} onChange={(v) => setCfg(setIn(cfg, ['instruments', i, 'trail', 'distance_atr'], v))} />
            <button type="button" onClick={() => setCfg(setIn(cfg, ['instruments', i, 'trail', 'break_even'], !ic.trail?.break_even))}
              className={`min-h-[44px] w-full rounded-xl border text-sm font-semibold ${ic.trail?.break_even ? 'border-buy/40 bg-buy/10 text-green-200' : 'border-white/10 bg-white/[0.03] text-slate-300'}`}>
              Break-even: {ic.trail?.break_even ? 'ON' : 'OFF'}
            </button>
          </div>
        )
      })}

      <ErrorBox message={err} />
      {disabledReason && <Notice tone="warn">{disabledReason}</Notice>}
      <div className="sticky bottom-3 z-10">
        <button className="btn-primary w-full" disabled={!dirty || busy || !!disabledReason} onClick={() => void save()}>
          <Save size={16} /> {busy ? 'Saving…' : dirty ? 'Save settings' : 'No changes'}
        </button>
      </div>
    </div>
  )
}

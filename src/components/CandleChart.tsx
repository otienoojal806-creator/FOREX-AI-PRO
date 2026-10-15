import type { Candle } from '../lib/types'

export interface Levels { entry?: number; tp?: number; sl?: number }

const UP = '#16c784'
const DOWN = '#ea3943'
const ENTRY = '#60a5fa'

// Dependency-free candlestick chart (SVG). Long-press / hover a candle for its OHLC values.
export default function CandleChart({ candles, levels, decimals }: { candles: Candle[]; levels?: Levels; decimals: number }) {
  const data = candles.slice(-100)
  if (data.length === 0) return null

  const W = 800, H = 340, PR = 74, PT = 12, PB = 26
  const plotW = W - PR
  const plotH = H - PT - PB
  const lv = [levels?.entry, levels?.tp, levels?.sl].filter((v): v is number => typeof v === 'number')
  const lo0 = Math.min(...data.map((c) => c.low), ...lv)
  const hi0 = Math.max(...data.map((c) => c.high), ...lv)
  const pad = (hi0 - lo0) * 0.05 || 1e-4
  const lo = lo0 - pad
  const hi = hi0 + pad
  const y = (v: number) => PT + ((hi - v) / (hi - lo)) * plotH
  const step = plotW / data.length
  const bw = Math.max(1.5, step * 0.62)
  const ticks = Array.from({ length: 5 }, (_, i) => lo + ((hi - lo) * i) / 4)
  const labelIdx = [0, 0.33, 0.66, 1].map((f) => Math.min(data.length - 1, Math.round(f * (data.length - 1))))
  const fmtT = (t: number) => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

  const line = (value: number | undefined, color: string, label: string) =>
    value == null ? null : (
      <g key={label}>
        <line x1={0} x2={plotW} y1={y(value)} y2={y(value)} stroke={color} strokeWidth={1.2} strokeDasharray="5 4" />
        <rect x={plotW + 2} y={y(value) - 8} width={PR - 4} height={16} rx={3} fill={color} />
        <text x={plotW + PR / 2} y={y(value) + 4} textAnchor="middle" fontSize={10} fontWeight={700} fill="#050b1a">
          {label} {value.toFixed(decimals)}
        </text>
      </g>
    )

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label="Price chart">
      {ticks.map((t, i) => (
        <g key={i}>
          <line x1={0} x2={plotW} y1={y(t)} y2={y(t)} stroke="#1f3260" strokeWidth={0.6} />
          <text x={plotW + 6} y={y(t) + 3} fontSize={10} fill="#94a3b8">{t.toFixed(decimals)}</text>
        </g>
      ))}
      {data.map((c, i) => {
        const x = i * step + step / 2
        const up = c.close >= c.open
        const color = up ? UP : DOWN
        const top = y(Math.max(c.open, c.close))
        const h = Math.max(1, Math.abs(y(c.open) - y(c.close)))
        return (
          <g key={c.time}>
            <title>{`${fmtT(c.time)}\nO ${c.open.toFixed(decimals)}  H ${c.high.toFixed(decimals)}\nL ${c.low.toFixed(decimals)}  C ${c.close.toFixed(decimals)}`}</title>
            <line x1={x} x2={x} y1={y(c.high)} y2={y(c.low)} stroke={color} strokeWidth={1} />
            <rect x={x - bw / 2} y={top} width={bw} height={h} fill={color} />
          </g>
        )
      })}
      {line(levels?.tp, UP, 'TP')}
      {line(levels?.entry, ENTRY, 'ENTRY')}
      {line(levels?.sl, DOWN, 'SL')}
      {labelIdx.map((i, k) => (
        <text key={k} x={Math.min(i * step + step / 2, plotW - 40)} y={H - 8} fontSize={10} fill="#94a3b8" textAnchor="middle">
          {fmtT(data[i].time)}
        </text>
      ))}
    </svg>
  )
}

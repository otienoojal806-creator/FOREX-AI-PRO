import { useEffect, useRef } from 'react'
import { ColorType, LineStyle, createChart } from 'lightweight-charts'
import type { IChartApi, IPriceLine, ISeriesApi, UTCTimestamp } from 'lightweight-charts'

export interface Candle { time: number; open: number; high: number; low: number; close: number }
export interface Level { price: number; color: string; title: string }

export default function PriceChart({ candles, levels = [], height = 380 }: { candles: Candle[]; levels?: Level[]; height?: number }) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const series = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const lines = useRef<IPriceLine[]>([])

  useEffect(() => {
    const el = box.current
    if (!el) return
    const c = createChart(el, {
      width: el.clientWidth,
      height,
      layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: '#16254a' }, horzLines: { color: '#16254a' } },
      rightPriceScale: { borderColor: '#1f3260' },
      timeScale: { borderColor: '#1f3260', timeVisible: true },
    })
    const s = c.addCandlestickSeries({
      upColor: '#16c784', downColor: '#ea3943',
      borderUpColor: '#16c784', borderDownColor: '#ea3943',
      wickUpColor: '#16c784', wickDownColor: '#ea3943',
    })
    chart.current = c
    series.current = s
    const ro = new ResizeObserver(() => c.applyOptions({ width: el.clientWidth }))
    ro.observe(el)
    return () => { ro.disconnect(); c.remove(); chart.current = null; series.current = null; lines.current = [] }
  }, [height])

  useEffect(() => {
    const s = series.current
    if (!s) return
    s.setData(candles.map((k) => ({ time: k.time as UTCTimestamp, open: k.open, high: k.high, low: k.low, close: k.close })))
    chart.current?.timeScale().fitContent()
  }, [candles])

  useEffect(() => {
    const s = series.current
    if (!s) return
    for (const l of lines.current) s.removePriceLine(l)
    lines.current = levels.map((lv) =>
      s.createPriceLine({ price: lv.price, color: lv.color, lineWidth: 2, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: lv.title }),
    )
  }, [levels, candles])

  return <div ref={box} className="w-full" style={{ height }} />
}

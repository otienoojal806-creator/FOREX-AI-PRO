import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Save, TrendingUp } from 'lucide-react'
import { supabase } from '../lib/supabase'
import { callFn } from '../lib/api'
import { decimals, errMsg, px } from '../lib/format'
import { INTERVALS, SYMBOLS, type AnalysisResult, type SignalRow } from '../lib/types'
import CandleChart from '../components/CandleChart'
import { ConfidenceRing, DirBadge, EmptyState, ErrorBox, Notice, PageHeader, SignalCard, Skeleton, Toast, fmtDate } from '../components/ui'

export default function AIAnalysis() {
  const [symbol, setSymbol] = useState<string>('EUR/USD')
  const [interval, setIntervalValue] = useState<string>('15min')
  const [result, setResult] = useState<AnalysisResult | null>(null)
  const [history, setHistory] = useState<SignalRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const loadHistory = useCallback(async () => {
    const { data, error } = await supabase.from('signals').select('*').order('created_at', { ascending: false }).limit(50)
    if (error) setErr(error.message)
    else setHistory(data as SignalRow[])
  }, [])

  const analyse = useCallback(async (save: boolean) => {
    setErr(null); setMsg(null)
    save ? setSaving(true) : setLoading(true)
    try {
      const r = await callFn<AnalysisResult>('analysis', { symbol, interval, save })
      setResult(r)
      if (save) {
        setMsg(r.signal ? 'Signal saved to your history.' : 'There is no signal to save right now.')
        await loadHistory()
      }
    } catch (e) {
      setErr(errMsg(e))
    } finally {
      setLoading(false); setSaving(false)
    }
  }, [symbol, interval, loadHistory])

  useEffect(() => { void loadHistory() }, [loadHistory])

  const d = decimals(symbol)
  const sig = result?.signal ?? null

  return (
    <>
      <PageHeader title="AI Analysis" subtitle="Live market data with BUY / SELL signals, entry, take-profit, stop-loss and confidence." />

      <div className="card mb-4 space-y-4">
        <div>
          <label className="label">Market</label>
          <div className="scroll-x" role="radiogroup" aria-label="Market">
            {SYMBOLS.map((s) => (
              <button key={s} type="button" role="radio" aria-checked={symbol === s} onClick={() => setSymbol(s)} className={`chip ${symbol === s ? 'chip-on' : ''}`}>{s}</button>
            ))}
          </div>
        </div>
        <div>
          <label className="label">Timeframe</label>
          <div className="scroll-x" role="radiogroup" aria-label="Timeframe">
            {INTERVALS.map((i) => (
              <button key={i} type="button" role="radio" aria-checked={interval === i} onClick={() => setIntervalValue(i)} className={`chip ${interval === i ? 'chip-on' : ''}`}>{i}</button>
            ))}
          </div>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row">
          <button className="btn-primary flex-1" disabled={loading} onClick={() => void analyse(false)}>
            <RefreshCw size={16} className={loading ? 'animate-spin' : ''} /> {result ? 'Refresh analysis' : 'Analyse market'}
          </button>
          {sig && (
            <button className="btn-ghost flex-1" disabled={saving} onClick={() => void analyse(true)}>
              <Save size={16} /> Save signal
            </button>
          )}
        </div>
      </div>

      <ErrorBox message={err} />
      <Toast message={msg} />

      {loading && !result && <div className="space-y-3"><Skeleton className="h-64" /><Skeleton className="h-40" /></div>}
      {!result && !loading && !err && (
        <Notice>Pick a market and timeframe, then tap Analyse market. Data is fetched live each time you refresh.</Notice>
      )}

      {result && (
        <div className="mt-4 grid gap-4 lg:grid-cols-3">
          <div className="card lg:col-span-2">
            <div className="mb-3 flex items-baseline justify-between">
              <h3 className="font-bold text-white">{result.symbol} · {result.interval}</h3>
              <span className="text-xl font-extrabold text-brand-cyan">{px(result.symbol, result.price)}</span>
            </div>
            <CandleChart candles={result.candles} decimals={d} levels={sig ? { entry: sig.entry, tp: sig.tp, sl: sig.sl } : undefined} />
          </div>

          <div className="space-y-4">
            <div className="card">
              <h3 className="mb-3 flex items-center gap-2 font-bold text-white"><TrendingUp size={16} className="text-brand-cyan" /> Signal</h3>
              {sig ? (
                <>
                  <div className="mb-4 flex items-center justify-between gap-3">
                    <div>
                      <DirBadge dir={sig.direction} />
                      <div className="mt-1 text-xs text-slate-400">Signal confidence</div>
                    </div>
                    <ConfidenceRing value={sig.confidence} dir={sig.direction} size={64} />
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-center">
                    <div className="rounded-xl bg-white/[0.05] p-2.5"><div className="text-[10px] uppercase tracking-wider text-slate-400">Entry</div><div className="mt-1 break-all text-sm font-bold text-white">{px(result.symbol, sig.entry)}</div></div>
                    <div className="rounded-xl bg-buy/10 p-2.5"><div className="text-[10px] uppercase tracking-wider text-slate-400">Take profit</div><div className="mt-1 break-all text-sm font-bold text-buy-400">{px(result.symbol, sig.tp)}</div></div>
                    <div className="rounded-xl bg-sell/10 p-2.5"><div className="text-[10px] uppercase tracking-wider text-slate-400">Stop loss</div><div className="mt-1 break-all text-sm font-bold text-sell-400">{px(result.symbol, sig.sl)}</div></div>
                  </div>
                  <ul className="mt-4 list-inside list-disc space-y-1 text-xs text-slate-400">
                    {sig.rationale.map((r) => <li key={r}>{r}</li>)}
                  </ul>
                </>
              ) : (
                <p className="text-sm text-slate-400">{result.reason ?? 'No clear setup right now.'} Waiting is a valid decision.</p>
              )}
            </div>

            <div className="card">
              <h3 className="mb-3 font-bold text-white">Indicators</h3>
              <dl className="grid grid-cols-2 gap-y-2 text-sm">
                <dt className="text-slate-400">EMA 20</dt><dd className="text-right text-white">{px(result.symbol, result.indicators.ema20)}</dd>
                <dt className="text-slate-400">EMA 50</dt><dd className="text-right text-white">{px(result.symbol, result.indicators.ema50)}</dd>
                <dt className="text-slate-400">RSI 14</dt><dd className="text-right text-white">{result.indicators.rsi14}</dd>
                <dt className="text-slate-400">ATR 14</dt><dd className="text-right text-white">{result.indicators.atr14}</dd>
                <dt className="text-slate-400">Score</dt><dd className="text-right text-white">{result.indicators.score}</dd>
              </dl>
            </div>
          </div>
        </div>
      )}

      {result && <p className="mt-4 text-xs text-slate-500">{result.note} Signals come from a rule-based indicator model, not machine learning, and past behaviour does not guarantee future results.</p>}

      <h3 className="mb-3 mt-8 text-lg font-bold text-white">Signal history</h3>
      {!history ? (
        <div className="grid gap-3 md:grid-cols-2"><Skeleton className="h-32" /><Skeleton className="h-32" /></div>
      ) : history.length === 0 ? (
        <div className="card"><EmptyState title="No saved signals yet" hint="Analyse a market and tap Save signal to keep it here." /></div>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {history.map((s) => (
            <SignalCard key={s.id} symbol={s.symbol} interval={s.interval} dir={s.direction} confidence={s.confidence}
              entry={px(s.symbol, s.entry)} tp={px(s.symbol, s.tp)} sl={px(s.symbol, s.sl)} time={fmtDate(s.created_at)} />
          ))}
        </div>
      )}
    </>
  )
}

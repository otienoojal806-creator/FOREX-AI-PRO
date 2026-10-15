import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Save } from 'lucide-react'
import { supabase } from '../lib/supabase'
import { callFn } from '../lib/api'
import { decimals, errMsg, px } from '../lib/format'
import { INTERVALS, SYMBOLS, type AnalysisResult, type SignalRow } from '../lib/types'
import CandleChart from '../components/CandleChart'
import { DirBadge, ErrorBox, Notice, PageHeader, fmtDate } from '../components/ui'

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

      <div className="card mb-5 flex flex-wrap items-end gap-3">
        <div>
          <label className="label">Market</label>
          <select className="input" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {SYMBOLS.map((s) => <option key={s}>{s}</option>)}
          </select>
        </div>
        <div>
          <label className="label">Timeframe</label>
          <select className="input" value={interval} onChange={(e) => setIntervalValue(e.target.value)}>
            {INTERVALS.map((i) => <option key={i}>{i}</option>)}
          </select>
        </div>
        <button className="btn-primary" disabled={loading} onClick={() => void analyse(false)}>
          <RefreshCw size={16} className={loading ? 'animate-spin' : ''} /> {result ? 'Refresh analysis' : 'Analyse market'}
        </button>
        {sig && (
          <button className="btn-ghost" disabled={saving} onClick={() => void analyse(true)}>
            <Save size={16} /> Save signal
          </button>
        )}
      </div>

      <ErrorBox message={err} />
      {msg && <div className="mb-4"><Notice tone="ok">{msg}</Notice></div>}

      {!result && !loading && !err && (
        <Notice>Pick a market and timeframe, then tap Analyse market. Data is fetched live each time you refresh.</Notice>
      )}

      {result && (
        <div className="grid gap-5 lg:grid-cols-3">
          <div className="card lg:col-span-2">
            <div className="mb-3 flex items-baseline justify-between">
              <h3 className="font-semibold text-white">{result.symbol} · {result.interval}</h3>
              <span className="text-lg font-bold text-white">{px(result.symbol, result.price)}</span>
            </div>
            <CandleChart candles={result.candles} decimals={d} levels={sig ? { entry: sig.entry, tp: sig.tp, sl: sig.sl } : undefined} />
          </div>

          <div className="space-y-5">
            <div className="card">
              <h3 className="mb-3 font-semibold text-white">Signal</h3>
              {sig ? (
                <>
                  <div className="mb-3 flex items-center justify-between">
                    <DirBadge dir={sig.direction} />
                    <span className="text-sm text-slate-400">Confidence <b className="text-white">{sig.confidence}%</b></span>
                  </div>
                  <div className="mb-4 h-2 overflow-hidden rounded-full bg-ink-900">
                    <div className={`h-full ${sig.direction === 'BUY' ? 'bg-buy' : 'bg-sell'}`} style={{ width: `${sig.confidence}%` }} />
                  </div>
                  <dl className="space-y-2 text-sm">
                    <div className="flex justify-between"><dt className="text-slate-400">Entry</dt><dd className="font-semibold text-white">{px(result.symbol, sig.entry)}</dd></div>
                    <div className="flex justify-between"><dt className="text-slate-400">Take profit</dt><dd className="font-semibold text-buy-400">{px(result.symbol, sig.tp)}</dd></div>
                    <div className="flex justify-between"><dt className="text-slate-400">Stop loss</dt><dd className="font-semibold text-sell-400">{px(result.symbol, sig.sl)}</dd></div>
                  </dl>
                  <ul className="mt-4 list-inside list-disc space-y-1 text-xs text-slate-400">
                    {sig.rationale.map((r) => <li key={r}>{r}</li>)}
                  </ul>
                </>
              ) : (
                <p className="text-sm text-slate-400">{result.reason ?? 'No clear setup right now.'} Waiting is a valid decision.</p>
              )}
            </div>

            <div className="card">
              <h3 className="mb-3 font-semibold text-white">Indicators</h3>
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

      <h3 className="mb-3 mt-8 text-lg font-semibold text-white">Signal history</h3>
      <div className="card overflow-x-auto p-0">
        <table className="w-full min-w-[640px]">
          <thead className="border-b border-ink-600/60">
            <tr><th className="th">Time</th><th className="th">Market</th><th className="th">Frame</th><th className="th">Signal</th><th className="th">Entry</th><th className="th">TP</th><th className="th">SL</th><th className="th">Conf.</th></tr>
          </thead>
          <tbody>
            {(history ?? []).map((s) => (
              <tr key={s.id} className="border-b border-ink-700/60 last:border-0">
                <td className="td">{fmtDate(s.created_at)}</td>
                <td className="td">{s.symbol}</td>
                <td className="td">{s.interval}</td>
                <td className="td"><DirBadge dir={s.direction} /></td>
                <td className="td">{px(s.symbol, s.entry)}</td>
                <td className="td">{px(s.symbol, s.tp)}</td>
                <td className="td">{px(s.symbol, s.sl)}</td>
                <td className="td">{s.confidence}%</td>
              </tr>
            ))}
            {history && history.length === 0 && <tr><td className="td text-slate-500" colSpan={8}>No saved signals yet. Analyse a market and tap Save signal.</td></tr>}
            {!history && <tr><td className="td text-slate-500" colSpan={8}>Loading…</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}

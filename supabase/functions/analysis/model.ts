// Shared indicator model. Copied alongside each edge function that imports it (analysis, trade).
export const SYMBOLS = ['EUR/USD', 'GBP/USD', 'USD/JPY', 'AUD/USD', 'USD/CAD', 'USD/CHF', 'NZD/USD', 'XAU/USD']
export const INTERVALS = ['5min', '15min', '30min', '1h', '4h', '1day']

export interface Candle { time: number; open: number; high: number; low: number; close: number }
export interface Signal { direction: 'BUY' | 'SELL'; entry: number; tp: number; sl: number; confidence: number; rationale: string[] }

export class DataError extends Error { constructor(public status: number, msg: string) { super(msg) } }

export const digits = (s: string) => (s.includes('JPY') ? 3 : s.startsWith('XAU') ? 2 : 5)
const round = (n: number, d: number) => Number(n.toFixed(d))

export async function fetchCandles(symbol: string, interval: string): Promise<Candle[]> {
  const key = Deno.env.get('TWELVE_DATA_API_KEY')
  if (!key) throw new DataError(503, 'Market data provider is not configured (TWELVE_DATA_API_KEY secret missing)')
  const r = await fetch(`https://api.twelvedata.com/time_series?symbol=${encodeURIComponent(symbol)}&interval=${interval}&outputsize=200&order=ASC&apikey=${key}`)
  const td = await r.json()
  if (td.status === 'error' || !Array.isArray(td.values)) throw new DataError(502, td.message ?? 'Market data unavailable')
  const rows = td.values as { datetime: string; open: string; high: string; low: string; close: string }[]
  const candles = rows.map((v) => ({
    time: Math.floor(new Date(v.datetime.replace(' ', 'T') + 'Z').getTime() / 1000),
    open: +v.open, high: +v.high, low: +v.low, close: +v.close,
  }))
  if (candles.length < 60) throw new DataError(422, 'Not enough data to analyse')
  return candles
}

function ema(values: number[], period: number): number[] {
  const k = 2 / (period + 1)
  const out: number[] = []
  values.forEach((v, i) => out.push(i === 0 ? v : v * k + out[i - 1] * (1 - k)))
  return out
}
function rsi(closes: number[], period = 14): number {
  let gain = 0, loss = 0
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1]
    if (d >= 0) gain += d; else loss -= d
  }
  gain /= period; loss /= period
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1]
    gain = (gain * (period - 1) + Math.max(d, 0)) / period
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period
  }
  return loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)
}
function atr(h: number[], l: number[], c: number[], period = 14): number {
  const tr: number[] = []
  for (let i = 1; i < c.length; i++) tr.push(Math.max(h[i] - l[i], Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1])))
  let a = tr.slice(0, period).reduce((x, y) => x + y, 0) / period
  for (let i = period; i < tr.length; i++) a = (a * (period - 1) + tr[i]) / period
  return a
}

export function analyse(candles: Candle[], symbol: string) {
  const closes = candles.map((c) => c.close)
  const highs = candles.map((c) => c.high)
  const lows = candles.map((c) => c.low)
  const e20 = ema(closes, 20), e50 = ema(closes, 50)
  const last = closes[closes.length - 1]
  const l20 = e20[e20.length - 1], l50 = e50[e50.length - 1]
  const r14 = rsi(closes), a14 = atr(highs, lows, closes)
  const slope = (e20[e20.length - 1] - e20[e20.length - 6]) / a14

  // Score in [-100, 100]. Positive = bullish.
  const why: string[] = []
  let score = 0
  if (l20 > l50) { score += 30; why.push('EMA20 above EMA50 (uptrend)') } else { score -= 30; why.push('EMA20 below EMA50 (downtrend)') }
  if (last > l50) { score += 10; why.push('Price above EMA50') } else { score -= 10; why.push('Price below EMA50') }
  if (slope > 0.3) { score += 15; why.push('EMA20 rising') } else if (slope < -0.3) { score -= 15; why.push('EMA20 falling') }
  if (r14 >= 50 && r14 <= 68) { score += 20; why.push(`RSI ${r14.toFixed(0)} shows bullish momentum`) }
  else if (r14 > 68) { score -= 10; why.push(`RSI ${r14.toFixed(0)} overbought, reversal risk`) }
  else if (r14 < 50 && r14 >= 32) { score -= 20; why.push(`RSI ${r14.toFixed(0)} shows bearish momentum`) }
  else { score += 10; why.push(`RSI ${r14.toFixed(0)} oversold, bounce risk`) }

  const d = digits(symbol)
  let signal: Signal | null = null
  if (Math.abs(score) >= 35) {
    const dir = score > 0 ? 'BUY' : 'SELL'
    const sgn = dir === 'BUY' ? 1 : -1
    signal = {
      direction: dir,
      entry: round(last, d),
      sl: round(last - sgn * 1.5 * a14, d),
      tp: round(last + sgn * 2.5 * a14, d),
      confidence: Math.round(Math.min(92, 50 + Math.abs(score) * 0.5)),
      rationale: why,
    }
  }
  return {
    price: round(last, d),
    indicators: { ema20: round(l20, d), ema50: round(l50, d), rsi14: round(r14, 1), atr14: round(a14, d + 1), score },
    signal,
    reason: signal ? null : 'No clear setup: trend and momentum disagree.',
  }
}

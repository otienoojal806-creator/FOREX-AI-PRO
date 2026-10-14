// AI Analysis: market data + indicator-based signal model. Requires an ACTIVE ai_analysis purchase.
// Secrets needed: TWELVE_DATA_API_KEY
import { createClient } from 'jsr:@supabase/supabase-js@2'
import { DataError, INTERVALS, SYMBOLS, analyse, fetchCandles } from './model.ts'

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
}
const json = (b: unknown, status = 200) =>
  new Response(JSON.stringify(b), { status, headers: { ...cors, 'Content-Type': 'application/json' } })

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors })
  try {
    const url = Deno.env.get('SUPABASE_URL')!
    const userClient = createClient(url, Deno.env.get('SUPABASE_ANON_KEY')!, {
      global: { headers: { Authorization: req.headers.get('Authorization') ?? '' } },
    })
    const { data: { user } } = await userClient.auth.getUser()
    if (!user) return json({ error: 'Not signed in' }, 401)

    const admin = createClient(url, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!)
    const { data: acc } = await admin.from('module_access').select('status').eq('user_id', user.id).eq('module', 'ai_analysis').maybeSingle()
    if (acc?.status !== 'active') return json({ error: 'AI Analysis is not activated for this account' }, 403)

    const body = await req.json().catch(() => ({}))
    const symbol: string = body.symbol ?? 'EUR/USD'
    const interval: string = body.interval ?? '15min'
    const save: boolean = body.save === true
    if (!SYMBOLS.includes(symbol)) return json({ error: 'Unsupported symbol' }, 400)
    if (!INTERVALS.includes(interval)) return json({ error: 'Unsupported interval' }, 400)

    const candles = await fetchCandles(symbol, interval)
    const result = analyse(candles, symbol)

    let signalId: string | null = null
    if (save && result.signal) {
      const { data } = await admin.from('signals').insert({ user_id: user.id, symbol, interval, ...result.signal }).select('id').single()
      signalId = data?.id ?? null
    }

    return json({
      symbol, interval, candles, ...result, signalId,
      note: 'Rule-based indicator model. Not financial advice; no signal is guaranteed.',
    })
  } catch (e) {
    if (e instanceof DataError) return json({ error: e.message }, e.status)
    return json({ error: e instanceof Error ? e.message : 'Unexpected error' }, 500)
  }
})

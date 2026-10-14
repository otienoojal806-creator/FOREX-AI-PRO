// Trade automation: simulated (default) and live (MetaApi / MT5) execution.
// Requires an ACTIVE auto_trading purchase. Secrets: TWELVE_DATA_API_KEY, BROKER_ENC_KEY.
// Live orders are placed ONLY when: broker connected + live consent recorded + settings.mode = 'live'
// + the request carries explicit confirmation (confirm:true) or settings.auto_enabled with auto:true.
import { createClient } from 'jsr:@supabase/supabase-js@2'
import { DataError, analyse, fetchCandles } from './model.ts'

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
}
const json = (b: unknown, status = 200) =>
  new Response(JSON.stringify(b), { status, headers: { ...cors, 'Content-Type': 'application/json' } })
class HttpError extends Error { constructor(public status: number, msg: string) { super(msg) } }

const SYMBOLS = ['EUR/USD', 'GBP/USD', 'USD/JPY', 'AUD/USD', 'USD/CAD', 'USD/CHF', 'NZD/USD', 'XAU/USD']
const REGIONS = ['new-york', 'london', 'singapore']
const SIM_START_BALANCE = 10000

const pipSize = (s: string) => (s.includes('JPY') ? 0.01 : s.startsWith('XAU') ? 0.1 : 0.0001)
const contract = (s: string) => (s.startsWith('XAU') ? 100 : 100000)
const digits = (s: string) => (s.includes('JPY') ? 3 : s.startsWith('XAU') ? 2 : 5)
const usdPer = (s: string, price: number) => (s.endsWith('/USD') ? 1 : 1 / price)
const rnd = (n: number, d: number) => Number(n.toFixed(d))
const pnlUsd = (s: string, entry: number, exit: number, dir: string, lots: number) =>
  (exit - entry) * (dir === 'BUY' ? 1 : -1) * contract(s) * lots * usdPer(s, exit)

async function getPrice(symbol: string): Promise<number> {
  const key = Deno.env.get('TWELVE_DATA_API_KEY')
  if (!key) throw new HttpError(503, 'Market data provider is not configured (TWELVE_DATA_API_KEY secret missing)')
  const r = await fetch(`https://api.twelvedata.com/price?symbol=${encodeURIComponent(symbol)}&apikey=${key}`)
  const j = await r.json()
  const p = Number(j.price)
  if (!p) throw new HttpError(502, j.message ?? 'Price unavailable')
  return p
}

// --- broker token encryption (AES-GCM) ---
async function cryptoKey() {
  const secret = Deno.env.get('BROKER_ENC_KEY')
  if (!secret) throw new HttpError(503, 'BROKER_ENC_KEY secret is not configured')
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret))
  return crypto.subtle.importKey('raw', hash, 'AES-GCM', false, ['encrypt', 'decrypt'])
}
const b64 = (u: Uint8Array) => btoa(String.fromCharCode(...u))
const unb64 = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0))
async function encrypt(plain: string) {
  const iv = crypto.getRandomValues(new Uint8Array(12))
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, await cryptoKey(), new TextEncoder().encode(plain)))
  const out = new Uint8Array(iv.length + ct.length); out.set(iv); out.set(ct, iv.length)
  return b64(out)
}
async function decrypt(enc: string) {
  const raw = unb64(enc)
  const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: raw.slice(0, 12) }, await cryptoKey(), raw.slice(12))
  return new TextDecoder().decode(pt)
}

// --- MetaApi (MT5) REST client ---
interface Broker { region: string; accountId: string; token: string }
async function meta(b: Broker, path: string, init?: { method?: string; body?: unknown }) {
  const base = `https://mt-client-api-v1.${b.region}.agiliumtrade.ai/users/current/accounts/${b.accountId}`
  const r = await fetch(base + path, {
    method: init?.method ?? 'GET',
    headers: { 'auth-token': b.token, 'Content-Type': 'application/json' },
    body: init?.body ? JSON.stringify(init.body) : undefined,
  })
  const text = await r.text()
  let data: any = null
  try { data = JSON.parse(text) } catch { /* keep text */ }
  if (!r.ok) throw new HttpError(502, `Broker error: ${data?.message ?? text.slice(0, 200)}`)
  return data
}

Deno.serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: cors })
  try {
    const url = Deno.env.get('SUPABASE_URL')!
    const userClient = createClient(url, Deno.env.get('SUPABASE_ANON_KEY')!, {
      global: { headers: { Authorization: req.headers.get('Authorization') ?? '' } },
    })
    const { data: { user } } = await userClient.auth.getUser()
    if (!user) throw new HttpError(401, 'Not signed in')

    const db = createClient(url, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!)
    const { data: acc } = await db.from('module_access').select('status').eq('user_id', user.id).eq('module', 'auto_trading').maybeSingle()
    if (acc?.status !== 'active') throw new HttpError(403, 'Automated Trading is not activated for this account')

    const body = await req.json().catch(() => ({}))
    const action: string = body.action

    const loadSettings = async () => {
      const { data } = await db.from('automation_settings').select('*').eq('user_id', user.id).maybeSingle()
      if (!data) throw new HttpError(400, 'Save your automation settings first')
      return data
    }
    const loadBroker = async (): Promise<Broker> => {
      const { data } = await db.from('broker_credentials').select('*').eq('user_id', user.id).maybeSingle()
      if (!data) throw new HttpError(400, 'No broker connected')
      return { region: data.region, accountId: data.account_id, token: await decrypt(data.token_enc) }
    }
    const simBalance = async () => {
      const { data } = await db.from('trades').select('pnl_usd').eq('user_id', user.id).eq('mode', 'simulated').eq('status', 'closed')
      return SIM_START_BALANCE + (data ?? []).reduce((s, r) => s + Number(r.pnl_usd ?? 0), 0)
    }

    // ---------- connect / disconnect broker ----------
    if (action === 'connect_broker') {
      const accountId = String(body.accountId ?? '').trim()
      const token = String(body.token ?? '').trim()
      const region = String(body.region ?? 'new-york')
      if (!/^[A-Za-z0-9-]{8,64}$/.test(accountId)) throw new HttpError(400, 'Invalid account ID')
      if (token.length < 20 || token.length > 8192) throw new HttpError(400, 'Invalid broker API token')
      if (!REGIONS.includes(region)) throw new HttpError(400, 'Unsupported region')
      const info = await meta({ region, accountId, token }, '/account-information')
      await db.from('broker_credentials').upsert({ user_id: user.id, region, account_id: accountId, token_enc: await encrypt(token) })
      await db.from('automation_settings').upsert({ user_id: user.id, broker_connected: true }, { onConflict: 'user_id' })
      await db.from('audit_logs').insert({ actor_id: user.id, action: 'broker.connect', entity: 'broker_credentials', entity_id: user.id, details: { region } })
      return json({ ok: true, balance: info.balance, currency: info.currency, leverage: info.leverage })
    }
    if (action === 'disconnect_broker') {
      await db.from('broker_credentials').delete().eq('user_id', user.id)
      await db.from('automation_settings').update({ broker_connected: false, mode: 'simulated', auto_enabled: false }).eq('user_id', user.id)
      await db.from('audit_logs').insert({ actor_id: user.id, action: 'broker.disconnect', entity: 'broker_credentials', entity_id: user.id, details: {} })
      return json({ ok: true })
    }

    // ---------- account summary ----------
    if (action === 'summary') {
      const s = await loadSettings()
      const out: Record<string, unknown> = { simBalance: rnd(await simBalance(), 2), mode: s.mode }
      if (s.mode === 'live' && s.broker_connected) {
        const info = await meta(await loadBroker(), '/account-information')
        out.live = { balance: info.balance, equity: info.equity, currency: info.currency }
      }
      return json(out)
    }

    // ---------- open trade ----------
    const openTrade = async (symbol: string, direction: string, opts: { confirm: boolean; auto: boolean; signalId: string | null }) => {
      if (!SYMBOLS.includes(symbol)) throw new HttpError(400, 'Unsupported symbol')
      if (direction !== 'BUY' && direction !== 'SELL') throw new HttpError(400, 'Direction must be BUY or SELL')
      const s = await loadSettings()

      const { count } = await db.from('trades').select('id', { count: 'exact', head: true }).eq('user_id', user.id).eq('status', 'open')
      if ((count ?? 0) >= s.max_open_trades) throw new HttpError(400, `Maximum open trades reached (${s.max_open_trades})`)
      const { count: same } = await db.from('trades').select('id', { count: 'exact', head: true }).eq('user_id', user.id).eq('status', 'open').eq('symbol', symbol)
      if ((same ?? 0) > 0) throw new HttpError(400, `A ${symbol} trade is already open`)

      const live = s.mode === 'live'
      let broker: Broker | null = null
      if (live) {
        if (!s.live_consent_at || !s.broker_connected) throw new HttpError(400, 'Live trading needs a connected broker and your recorded consent')
        const authorised = opts.confirm || (opts.auto && s.auto_enabled === true)
        if (!authorised) throw new HttpError(400, 'Live orders require your explicit confirmation')
        broker = await loadBroker()
      }

      const price = await getPrice(symbol)
      const sgn = direction === 'BUY' ? 1 : -1
      const d = digits(symbol)
      const sl = rnd(price - sgn * s.sl_pips * pipSize(symbol), d)
      const tp = rnd(price + sgn * s.tp_pips * pipSize(symbol), d)

      // Risk cap: worst-case loss at SL must not exceed risk_pct of balance.
      const balance = live ? Number((await meta(broker!, '/account-information')).balance) : await simBalance()
      const riskUsd = s.sl_pips * pipSize(symbol) * contract(symbol) * Number(s.lot_size) * usdPer(symbol, price)
      if (riskUsd > (balance * Number(s.risk_pct)) / 100) {
        throw new HttpError(400, `Trade rejected by risk limit: worst case $${riskUsd.toFixed(2)} exceeds ${s.risk_pct}% of balance ($${(balance * Number(s.risk_pct) / 100).toFixed(2)}). Reduce lot size or stop-loss.`)
      }

      let positionId: string | null = null
      if (live) {
        const res = await meta(broker!, '/trade', {
          method: 'POST',
          body: { actionType: direction === 'BUY' ? 'ORDER_TYPE_BUY' : 'ORDER_TYPE_SELL', symbol: symbol.replace('/', ''), volume: Number(s.lot_size), stopLoss: sl, takeProfit: tp, comment: 'ForexAI Pro' },
        })
        positionId = String(res.positionId ?? res.orderId ?? '')
        if (!positionId) throw new HttpError(502, 'Broker did not confirm the order')
      }

      const { data: trade, error } = await db.from('trades').insert({
        user_id: user.id, mode: s.mode, symbol, direction, lots: s.lot_size, entry_price: price, sl, tp,
        trailing_pips: s.trailing_pips, broker_position_id: positionId, signal_id: opts.signalId,
      }).select('*').single()
      if (error) throw new HttpError(500, error.message)
      await db.from('audit_logs').insert({ actor_id: user.id, action: live ? 'trade.open_live' : 'trade.open_sim', entity: 'trades', entity_id: trade.id, details: { symbol, direction, lots: s.lot_size, auto: opts.auto } })
      return trade
    }

    if (action === 'open') {
      const trade = await openTrade(String(body.symbol), String(body.direction), { confirm: body.confirm === true, auto: false, signalId: null })
      return json({ ok: true, trade })
    }

    // Auto mode: the browser calls this on a timer while auto-trading is enabled. The server decides
    // from its own indicator model; it never trusts a client-supplied direction.
    if (action === 'auto_scan') {
      const s = await loadSettings()
      if (!s.auto_enabled) throw new HttpError(400, 'Auto-trading is not enabled')
      const symbol = String(body.symbol)
      if (!(s.symbols as string[]).includes(symbol)) throw new HttpError(400, 'Symbol is not in your auto-trading list')
      const result = analyse(await fetchCandles(symbol, '15min'), symbol)
      const sig = result.signal
      if (!sig) return json({ ok: true, action: 'none', reason: result.reason })
      if (sig.confidence < s.min_confidence) return json({ ok: true, action: 'none', reason: `Confidence ${sig.confidence}% is below your ${s.min_confidence}% minimum`, signal: sig })
      try {
        const trade = await openTrade(symbol, sig.direction, { confirm: false, auto: true, signalId: null })
        return json({ ok: true, action: 'opened', trade, signal: sig })
      } catch (e) {
        if (e instanceof HttpError && e.status === 400) return json({ ok: true, action: 'skipped', reason: e.message, signal: sig })
        throw e
      }
    }

    // ---------- close trade ----------
    if (action === 'close') {
      const { data: t } = await db.from('trades').select('*').eq('id', body.tradeId).eq('user_id', user.id).eq('status', 'open').maybeSingle()
      if (!t) throw new HttpError(404, 'Open trade not found')
      let closePrice: number | null = null
      if (t.mode === 'live') {
        await meta(await loadBroker(), '/trade', { method: 'POST', body: { actionType: 'POSITION_CLOSE_ID', positionId: t.broker_position_id } })
        closePrice = await getPrice(t.symbol)
      } else {
        closePrice = await getPrice(t.symbol)
      }
      const pnl = rnd(pnlUsd(t.symbol, Number(t.entry_price), closePrice, t.direction, Number(t.lots)), 2)
      await db.from('trades').update({ status: 'closed', close_price: closePrice, pnl_usd: pnl, close_reason: 'manual', closed_at: new Date().toISOString() }).eq('id', t.id)
      await db.from('audit_logs').insert({ actor_id: user.id, action: 'trade.close', entity: 'trades', entity_id: t.id, details: { mode: t.mode, pnl } })
      return json({ ok: true, pnl })
    }

    // ---------- monitor open trades (SL / TP / trailing) ----------
    if (action === 'monitor') {
      const { data: open } = await db.from('trades').select('*').eq('user_id', user.id).eq('status', 'open')
      const trades = open ?? []
      const prices: Record<string, number> = {}
      for (const sym of new Set(trades.map((t) => t.symbol))) prices[sym] = await getPrice(sym)

      let livePositions: Set<string> | null = null
      let broker: Broker | null = null
      if (trades.some((t) => t.mode === 'live')) {
        broker = await loadBroker()
        const pos = await meta(broker, '/positions')
        livePositions = new Set((pos as { id: string }[]).map((p) => String(p.id)))
      }

      const out = []
      for (const t of trades) {
        const p = prices[t.symbol]
        const sgn = t.direction === 'BUY' ? 1 : -1
        const pip = pipSize(t.symbol)
        let sl = Number(t.sl)
        const tp = Number(t.tp)
        const entry = Number(t.entry_price)

        // Trailing stop: once in profit by trailing_pips, keep SL trailing_pips behind price.
        if (t.trailing_pips > 0 && ((p - entry) * sgn) / pip >= t.trailing_pips) {
          const newSl = rnd(p - sgn * t.trailing_pips * pip, digits(t.symbol))
          if ((newSl - sl) * sgn > 0) {
            sl = newSl
            await db.from('trades').update({ sl }).eq('id', t.id)
            if (t.mode === 'live' && broker) {
              await meta(broker, '/trade', { method: 'POST', body: { actionType: 'POSITION_MODIFY', positionId: t.broker_position_id, stopLoss: sl, takeProfit: tp } })
            }
          }
        }

        let closed: { price: number | null; reason: string } | null = null
        if (t.mode === 'live') {
          if (livePositions && !livePositions.has(String(t.broker_position_id))) closed = { price: null, reason: 'closed at broker' }
        } else if ((p - sl) * sgn <= 0) closed = { price: sl, reason: 'stop-loss' }
        else if ((p - tp) * sgn >= 0) closed = { price: tp, reason: 'take-profit' }

        if (closed) {
          const pnl = closed.price != null ? rnd(pnlUsd(t.symbol, entry, closed.price, t.direction, Number(t.lots)), 2) : null
          await db.from('trades').update({ status: 'closed', close_price: closed.price, pnl_usd: pnl, close_reason: closed.reason, closed_at: new Date().toISOString() }).eq('id', t.id)
        } else {
          out.push({ id: t.id, price: p, sl, unrealisedUsd: rnd(pnlUsd(t.symbol, entry, p, t.direction, Number(t.lots)), 2) })
        }
      }
      return json({ ok: true, open: out })
    }

    throw new HttpError(400, 'Unknown action')
  } catch (e) {
    if (e instanceof HttpError || e instanceof DataError) return json({ error: e.message }, e.status)
    return json({ error: e instanceof Error ? e.message : 'Unexpected error' }, 500)
  }
})

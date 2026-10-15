export type ModuleKey = 'ai_analysis' | 'auto_trading'
export type PaymentStatus = 'pending' | 'verified' | 'rejected'
export type AccessStatus = 'active' | 'revoked'
export type AppRole = 'admin' | 'user'
export type TradeMode = 'simulated' | 'live'

export const MODULES: Record<ModuleKey, { title: string; blurb: string; features: string[] }> = {
  ai_analysis: {
    title: 'AI Analysis',
    blurb: 'Live market data and AI-generated BUY/SELL signals.',
    features: ['Live market charts', 'BUY / SELL signals', 'Entry, take-profit and stop-loss levels', 'Confidence scores', 'Signal history'],
  },
  auto_trading: {
    title: 'Automated Trading',
    blurb: 'Broker-connected execution with risk controls. Simulated mode first.',
    features: ['MT5 / broker connection', 'Lot size and risk limits', 'Stop-loss, take-profit, trailing stop', 'Open-trade monitoring', 'Explicit consent required for live trading'],
  },
}

export interface PaymentSetting {
  module: ModuleKey
  binance_url: string
  price_usd: number
  updated_by?: string | null
  updated_at: string
}

export interface ModuleAccess {
  user_id: string
  module: ModuleKey
  status: AccessStatus
  source_submission?: string | null
  activated_by?: string | null
  activated_at: string
  revoked_at: string | null
  revoke_reason: string | null
}

export interface PaymentSubmission {
  id: string
  user_id: string
  module: ModuleKey
  tx_id: string
  proof_path: string | null
  note: string | null
  status: PaymentStatus
  verified_amount_usd: number | null
  admin_note: string | null
  reviewed_by?: string | null
  reviewed_at: string | null
  created_at: string
}

export interface Profile { id: string; email: string; full_name: string | null; created_at: string }

export interface AuditLog {
  id: number
  actor_id: string | null
  action: string
  entity: string
  entity_id: string | null
  details: Record<string, unknown>
  created_at: string
}

// Aliases used across the existing pages.
export type PaymentSettings = PaymentSetting
export type Access = ModuleAccess
export type Submission = PaymentSubmission

// ---- Trading ----
export const SYMBOLS = ['EUR/USD', 'GBP/USD', 'USD/JPY', 'AUD/USD', 'USD/CAD', 'USD/CHF', 'NZD/USD', 'XAU/USD'] as const
export const INTERVALS = ['5min', '15min', '30min', '1h', '4h', '1day'] as const

export interface Candle { time: number; open: number; high: number; low: number; close: number }

export interface SignalData {
  direction: 'BUY' | 'SELL'
  entry: number
  tp: number
  sl: number
  confidence: number
  rationale: string[]
}

export interface AnalysisResult {
  symbol: string
  interval: string
  candles: Candle[]
  price: number
  indicators: { ema20: number; ema50: number; rsi14: number; atr14: number; score: number }
  signal: SignalData | null
  reason: string | null
  signalId: string | null
  note: string
}

export interface SignalRow extends SignalData { id: string; symbol: string; interval: string; created_at: string }

export interface AutomationSettings {
  user_id: string
  mode: TradeMode
  lot_size: number
  risk_pct: number
  sl_pips: number
  tp_pips: number
  trailing_pips: number
  max_open_trades: number
  min_confidence: number
  symbols: string[]
  auto_enabled: boolean
  live_consent_at: string | null
  broker_connected: boolean
  updated_at: string
}

export interface TradeRow {
  id: string
  mode: TradeMode
  symbol: string
  direction: 'BUY' | 'SELL'
  lots: number
  entry_price: number
  sl: number
  tp: number
  trailing_pips: number
  status: 'open' | 'closed' | 'pending'
  close_price: number | null
  pnl_usd: number | null
  close_reason: string | null
  broker_position_id: string | null
  opened_at: string
  closed_at: string | null
}

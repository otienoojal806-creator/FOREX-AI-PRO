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

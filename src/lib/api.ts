import { FunctionsHttpError } from '@supabase/supabase-js'
import { supabase } from './supabase'
import { SYMBOLS as SYMBOL_LIST } from './types'

export { decimals } from './format'
export const SYMBOLS: readonly string[] = SYMBOL_LIST

/** Calls an edge function and turns non-2xx responses into Errors carrying the server's message. */
export async function callFn<T = any>(name: 'analysis' | 'trade', body: Record<string, unknown>): Promise<T> {
  const { data, error } = await supabase.functions.invoke(name, { body })
  if (error) {
    let msg = error.message
    if (error instanceof FunctionsHttpError) {
      try {
        const j = await error.context.json()
        if (j?.error) msg = String(j.error)
      } catch { /* keep the generic message */ }
    }
    throw new Error(msg)
  }
  return data as T
}

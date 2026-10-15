export const decimals = (symbol: string) => (symbol.includes('JPY') ? 3 : symbol.startsWith('XAU') ? 2 : 5)
export const px = (symbol: string, n: number | null | undefined) => (n == null ? '—' : Number(n).toFixed(decimals(symbol)))
export const errMsg = (e: unknown) => (e instanceof Error ? e.message : 'Something went wrong')

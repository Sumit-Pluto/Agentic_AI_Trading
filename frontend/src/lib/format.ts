export const fmt = (v: unknown, d = 1): string =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? 'n/a' : Number(v).toFixed(d)

export function numOr(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

export function hhmmss(iso?: string | null): string {
  if (!iso) return '–'
  const t = iso.indexOf('T')
  return t >= 0 ? iso.slice(t + 1, t + 9) : iso
}

/* Bar-time epochs: /api/candles stamps naive-IST bar times AS IF UTC (pandas
   Timestamp.timestamp() semantics). Any time a trigger/entry/exit time is
   matched onto chart bars it MUST be parsed the same way, otherwise every
   marker shifts 5.5h and piles up at the 09:15 open. */
export function parseBarTime(iso?: string | null): number {
  if (!iso) return NaN
  const s = String(iso)
  const hasTz = /[Zz]$|[+-]\d\d:?\d\d$/.test(s)
  return Date.parse(hasTz ? s : s + 'Z') / 1000
}

export function inr(v: unknown): string {
  const n = numOr(v)
  if (n === null) return 'n/a'
  const sign = n < 0 ? '−' : ''
  return sign + '₹' + Math.abs(Math.round(n)).toLocaleString('en-IN')
}

export function pctIv(v: unknown): string {
  const n = numOr(v)
  if (n === null) return 'n/a'
  return (n > 3 ? n : n * 100).toFixed(1) + '%'
}

export function signed(v: unknown, d = 2): string {
  const n = numOr(v)
  return n === null ? '–' : (n >= 0 ? '+' : '') + n.toFixed(d)
}

export function pnlCls(v: unknown): string {
  const n = numOr(v)
  return n === null || n === 0 ? '' : n > 0 ? 'text-green' : 'text-red'
}

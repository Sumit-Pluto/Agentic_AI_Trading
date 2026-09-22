export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export async function jget<T = any>(url: string): Promise<T> {
  const r = await fetch(url)
  const body = await r.json().catch(() => ({}))
  if (!r.ok) throw new ApiError(body.error || body.detail || r.statusText, r.status)
  return body as T
}

export async function jpost<T = any>(url: string, payload: unknown): Promise<T> {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body = await r.json().catch(() => ({}))
  if (!r.ok) throw new ApiError(body.error || body.detail || r.statusText, r.status)
  return body as T
}

export function wlAdd(item: { symbol: string; strategy: string; direction: string; segment: string }) {
  return jpost('/api/watchlist', item)
}

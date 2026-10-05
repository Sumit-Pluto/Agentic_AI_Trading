import { useEffect, useRef, useState } from 'react'

// ---- snapshot types (mirror intraday/server/runner._snapshot) ----
export interface Regime { on?: boolean; scalar?: number; detail?: string; vetoes?: string[]; avg?: number }
export interface EngineState {
  mode?: string; trading_mode?: string; broker?: string; cap_state?: string; data_source?: string; live_data?: boolean
  paused?: boolean; halted?: boolean; now?: string; square_off?: boolean
  equity?: number | null; realized?: number | null; unrealized?: number | null
  day_pnl?: number | null; n_positions?: number; entries?: number; vix?: number | null; regime?: Regime
}
export interface Budget {
  total?: number | null; deployed?: number | null; utilisation_pct?: number | null
  cap_state?: string; soft_cap_pct?: number | null; hard_cap_pct?: number | null
}
export interface Position {
  symbol: string; underlying: string; side: string; right: string; strike: number
  qty: number; lots: number; entry_px: number | null; mark: number | null; pnl: number | null
  stop: number | null; age_bars: number; strategy: string; entry_ts: string
}
export interface Signal {
  ts: string; symbol: string; direction: string; score_buy: number; score_sell: number
  family_scores: Record<string, number>; instrument: Record<string, unknown> | null
}
export interface AgentRow {
  symbol: string; agent: string; family: string; score_buy: number | null; score_sell: number | null
  na: string | null; veto: string | null; veto_long: string | null; veto_short: string | null
  shadow: boolean; detail: string
}
export interface Leg {
  ltp: number | null; bid: number | null; ask: number | null; oi: number; d_oi: number
  volume: number; iv: number | null; delta: number | null
}
export interface ChainRow { strike: number; ce: Leg | null; pe: Leg | null }
export interface Chain {
  symbol: string; spot: number | null; atm: number | null; expiry: string; lot_size: number
  features: Record<string, number | null>; rows: ChainRow[]
}
export interface ActivityEvent { ts: string; stage: string; msg: string }
export interface ScanProg { symbol: string; role: string; bars: number; chain: boolean }
export interface Snapshot {
  type?: string; seq: number; state: EngineState; positions: Position[]; signals: Signal[]
  agent_rows: AgentRow[]; chains: Record<string, Chain>; equity_point?: { t: string; equity: number | null }
  budget?: Budget; funds?: Record<string, unknown> | null
  activity?: ActivityEvent[]; scan?: ScanProg[]
}

// ---- REST ----
export async function apiGet<T>(path: string): Promise<T> {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${path} → ${r.status}`)
  return r.json() as Promise<T>
}
export async function apiPost<T>(path: string, body: unknown, retry = true): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const tok = localStorage.getItem('intraday_token')
  if (tok) headers['Authorization'] = `Bearer ${tok}`
  const r = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) })
  if (r.status === 401 && retry) {
    // server has INTRADAY_API_TOKEN set: prompt once, remember, retry
    const t = window.prompt('API token required (server INTRADAY_API_TOKEN):')
    if (t) {
      localStorage.setItem('intraday_token', t)
      return apiPost<T>(path, body, false)
    }
  }
  return r.json() as Promise<T>
}

// ---- live WebSocket ----
function wsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/ws`
}

export function useLive() {
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const [connected, setConnected] = useState(false)
  const equityRef = useRef<{ t: string; equity: number }[]>([])
  const [equity, setEquity] = useState<{ t: string; equity: number }[]>([])

  useEffect(() => {
    let ws: WebSocket | null = null
    let stop = false
    let backoff = 500
    const connect = () => {
      ws = new WebSocket(wsUrl())
      ws.onopen = () => { setConnected(true); backoff = 500 }
      ws.onmessage = (ev) => {
        try {
          const s = JSON.parse(ev.data) as Snapshot
          setSnap(s)
          const ep = s.equity_point
          if (ep && ep.equity != null) {
            equityRef.current = [...equityRef.current.slice(-400), { t: ep.t, equity: ep.equity }]
            setEquity(equityRef.current)
          }
        } catch { /* ignore */ }
      }
      ws.onclose = () => {
        setConnected(false)
        if (!stop) setTimeout(connect, (backoff = Math.min(backoff * 2, 8000)))
      }
      ws.onerror = () => ws?.close()
    }
    connect()
    return () => { stop = true; ws?.close() }
  }, [])

  return { snap, connected, equity }
}

// ---- formatting ----
export function inr(n: number | null | undefined, dp = 0): string {
  if (n == null || !isFinite(n)) return '—'
  const a = Math.abs(n)
  if (a >= 1e7) return `₹${(n / 1e7).toFixed(2)}Cr`
  if (a >= 1e5) return `₹${(n / 1e5).toFixed(2)}L`
  return `₹${n.toLocaleString('en-IN', { maximumFractionDigits: dp })}`
}
export function signed(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '—'
  return (n >= 0 ? '+' : '') + inr(n)
}
export function pnlClass(n: number | null | undefined): string {
  if (n == null) return 'dim'
  return n > 0 ? 'pos' : n < 0 ? 'neg' : 'dim'
}
export function num(n: number | null | undefined, dp = 2): string {
  if (n == null || !isFinite(n)) return '—'
  return n.toFixed(dp)
}
export function hhmm(iso?: string): string {
  // server timestamps are IST-aware (…T16:58:22+05:30). Show the wall-clock HH:MM
  // AS-IS — do NOT new Date().toISOString() (that converts to UTC and shows 11:28).
  if (!iso) return '—'
  const m = /T(\d{2}:\d{2})/.exec(iso)
  return m ? m[1] : iso.slice(0, 5)
}

import { useEffect, useState } from 'react'
import { apiGet, hhmm, num, pnlClass, signed } from '../lib/live'

interface Trade {
  id: number; symbol: string; underlying: string; side: string; strategy: string
  entry_ts: string; exit_ts: string; entry_px: number; exit_px: number; qty: number
  pnl: number; r: number; exit_reason: string
}
interface Order {
  id: number; symbol: string; side: string; qty: number; order_type: string; status: string
  latency_ms: number | null; reason: string; date: string
}

function usePoll<T>(path: string, ms = 3000): T | null {
  const [data, setData] = useState<T | null>(null)
  useEffect(() => {
    let alive = true
    const tick = () => apiGet<T>(path).then((d) => alive && setData(d)).catch(() => {})
    tick(); const h = setInterval(tick, ms)
    return () => { alive = false; clearInterval(h) }
  }, [path, ms])
  return data
}

export function Journal() {
  const trades = usePoll<{ trades: Trade[] }>('/api/trades')?.trades ?? []
  const orders = usePoll<{ orders: Order[] }>('/api/orders')?.orders ?? []
  return (
    <div style={{ display: 'grid', gap: 12, gridTemplateColumns: '1.4fr 1fr', height: '100%', minHeight: 0 }}>
      <div className="card scroll" style={{ minHeight: 0 }}>
        <table>
          <thead><tr><th>Exit</th><th>Instrument</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>R</th><th style={{ textAlign: 'left' }}>Reason</th></tr></thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.id}>
                <td className="mono dim">{hhmm(t.exit_ts)}</td>
                <td className="mono">{t.symbol}</td>
                <td>{t.side}</td>
                <td className="mono">{t.qty}</td>
                <td className="mono">{num(t.entry_px)}</td>
                <td className="mono">{num(t.exit_px)}</td>
                <td className={`mono ${pnlClass(t.pnl)}`}>{signed(t.pnl)}</td>
                <td className="mono dim">{num(t.r, 1)}</td>
                <td className="dim">{t.exit_reason}</td>
              </tr>
            ))}
            {trades.length === 0 && <tr><td colSpan={9} className="dim">no closed trades yet</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="card scroll" style={{ minHeight: 0 }}>
        <table>
          <thead><tr><th style={{ textAlign: 'left' }}>Order</th><th>Side</th><th>Qty</th><th>Status</th><th>Lat</th></tr></thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td className="mono" style={{ textAlign: 'left' }}>{o.symbol}</td>
                <td>{o.side}</td>
                <td className="mono">{o.qty}</td>
                <td><span className="pill" style={{ color: o.status === 'FILLED' ? 'var(--green)' : o.status.startsWith('BLOCK') ? 'var(--amber)' : 'var(--dim)' }}>{o.status}</span></td>
                <td className="mono dim">{o.latency_ms != null ? `${Math.round(o.latency_ms)}ms` : '—'}</td>
              </tr>
            ))}
            {orders.length === 0 && <tr><td colSpan={5} className="dim">no orders yet</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

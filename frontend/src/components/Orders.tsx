import { useEffect, useState } from 'react'
import { apiGet, num, pnlClass, signed, type BrokerOrder, type BrokerPosition, type Snapshot } from '../lib/live'

const OPEN = new Set(['OPEN', 'PENDING', 'TRIGGER_PENDING', 'INCOMPLETE'])

function usePoll<T>(path: string, ms: number): T | null {
  const [data, setData] = useState<T | null>(null)
  useEffect(() => {
    let alive = true
    const tick = () => apiGet<T>(path).then((d) => alive && setData(d)).catch(() => {})
    tick(); const h = setInterval(tick, ms)
    return () => { alive = false; clearInterval(h) }
  }, [path, ms])
  return data
}

function statusColor(s: string): string {
  if (s === 'COMPLETE' || s === 'TRADED') return 'var(--green)'
  if (OPEN.has(s)) return 'var(--amber)'
  if (s.startsWith('REJECT') || s.startsWith('CANCEL')) return 'var(--red)'
  return 'var(--dim)'
}

export function OrdersView({ snap }: { snap: Snapshot | null }) {
  const book = usePoll<{ connected: boolean; orders: BrokerOrder[] }>('/api/orderbook', 3000)
  const bpos = usePoll<{ connected: boolean; positions: BrokerPosition[] }>('/api/broker-positions', 5000)
  const connected = book?.connected === true
  const orders = book?.orders ?? []
  const open = orders.filter((o) => OPEN.has(o.status))
  const done = orders.filter((o) => !OPEN.has(o.status))
  const engine = new Set((snap?.positions ?? []).map((p) => p.symbol))

  if (book && !connected) {
    return <div className="card" style={{ padding: 16 }}><span className="dim">
      No Gateway client on this server — live orders need engine_mode=live with broker credentials. Journal shows the engine's own history.</span></div>
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, height: '100%', minHeight: 0 }}>
      <div className="card scroll" style={{ minHeight: 0, maxHeight: 220 }}>
        <table>
          <thead><tr><th>Open Orders ({open.length})</th><th>Side</th><th>Qty</th><th>Filled</th>
            <th>Price</th><th>Avg</th><th>Status</th><th>Time</th><th style={{ textAlign: 'left' }}>Remarks</th></tr></thead>
          <tbody>
            {open.map((o) => <OrderRow key={o.id} o={o} />)}
            {open.length === 0 && <tr><td colSpan={9} className="dim">no working orders at the broker</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="card scroll" style={{ minHeight: 0, maxHeight: 220 }}>
        <table>
          <thead><tr><th>Today's History ({done.length})</th><th>Side</th><th>Qty</th><th>Filled</th>
            <th>Price</th><th>Avg</th><th>Status</th><th>Time</th><th style={{ textAlign: 'left' }}>Remarks</th></tr></thead>
          <tbody>
            {done.map((o) => <OrderRow key={o.id} o={o} />)}
            {done.length === 0 && <tr><td colSpan={9} className="dim">no completed orders yet today</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="card scroll" style={{ minHeight: 0, flex: 1 }}>
        <table>
          <thead><tr><th>Broker Positions ({(bpos?.positions ?? []).length})</th><th>Side</th><th>Qty</th>
            <th>Avg</th><th>LTP</th><th>MTM</th><th>P&amp;L</th><th>Engine</th></tr></thead>
          <tbody>
            {(bpos?.positions ?? []).map((p, i) => (
              <tr key={`${p.tsym}-${p.side}-${i}`}>
                <td className="mono">{p.tsym}</td>
                <td className="dim">{p.side}</td>
                <td className="mono">{p.qty}</td>
                <td className="mono">{num(p.avg)}</td>
                <td className="mono">{num(p.ltp)}</td>
                <td className={`mono ${pnlClass(p.mtm)}`}>{signed(p.mtm)}</td>
                <td className={`mono ${pnlClass(p.pnl)}`}>{signed(p.pnl)}</td>
                <td>{engine.has(p.tsym)
                  ? <span className="pill" style={{ color: 'var(--green)' }}>tracked</span>
                  : <span className="pill dim" title="manual trade or another strategy — engine does not manage it">external</span>}</td>
              </tr>
            ))}
            {(bpos?.positions ?? []).length === 0 && <tr><td colSpan={8} className="dim">no open positions at the broker</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function OrderRow({ o }: { o: BrokerOrder }) {
  const note = o.rejreason || o.remarks
  return (
    <tr>
      <td className="mono">{o.tsym}</td>
      <td className="dim">{o.side}</td>
      <td className="mono">{o.qty}</td>
      <td className="mono">{o.filled}</td>
      <td className="mono">{num(o.price)}</td>
      <td className="mono">{num(o.avg)}</td>
      <td><span className="pill" style={{ color: statusColor(o.status) }}>{o.status}</span></td>
      <td className="mono dim">{o.time || '—'}</td>
      <td className="dim wrap" style={{ textAlign: 'left' }}>{note || '—'}</td>
    </tr>
  )
}

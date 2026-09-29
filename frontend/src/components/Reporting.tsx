import { useEffect, useState } from 'react'
import { apiGet, inr, pnlClass, signed } from '../lib/live'

interface Group { n: number; pnl: number; wins: number }
interface Report {
  trades: number; wins: number; win_rate: number; total_pnl: number; avg_win: number
  avg_loss: number; max_drawdown: number; avg_latency_ms: number | null
  by_strategy: Record<string, Group>; by_symbol: Record<string, Group>; by_exit: Record<string, Group>
}

export function Reporting() {
  const [r, setR] = useState<Report | null>(null)
  useEffect(() => {
    const tick = () => apiGet<Report>('/api/reporting').then(setR).catch(() => {})
    tick(); const h = setInterval(tick, 5000); return () => clearInterval(h)
  }, [])
  if (!r) return <div className="dim">loading…</div>

  const tiles = [
    { k: 'Trades', v: String(r.trades) },
    { k: 'Win rate', v: `${r.win_rate}%` },
    { k: 'Total P&L', v: signed(r.total_pnl), c: pnlClass(r.total_pnl) },
    { k: 'Avg win', v: inr(r.avg_win), c: 'pos' },
    { k: 'Avg loss', v: inr(r.avg_loss), c: 'neg' },
    { k: 'Max drawdown', v: inr(r.max_drawdown), c: 'neg' },
    { k: 'Avg latency', v: r.avg_latency_ms != null ? `${r.avg_latency_ms}ms` : '—' },
  ]
  return (
    <div style={{ display: 'grid', gap: 12, height: '100%', minHeight: 0, gridTemplateRows: 'auto 1fr' }}>
      <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fit,minmax(140px,1fr))' }}>
        {tiles.map((t) => (
          <div key={t.k} className="card" style={{ padding: '12px 14px' }}>
            <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase' }}>{t.k}</div>
            <div className={`mono ${t.c || ''}`} style={{ fontSize: 20, fontWeight: 700, marginTop: 4 }}>{t.v}</div>
          </div>
        ))}
      </div>
      <div className="scroll" style={{ minHeight: 0, display: 'grid', gap: 12, gridTemplateColumns: 'repeat(3,1fr)', alignContent: 'start' }}>
        <Breakdown title="By strategy" g={r.by_strategy} />
        <Breakdown title="By symbol" g={r.by_symbol} />
        <Breakdown title="By exit reason" g={r.by_exit} />
      </div>
    </div>
  )
}

function Breakdown({ title, g }: { title: string; g: Record<string, Group> }) {
  const rows = Object.entries(g).sort((a, b) => b[1].pnl - a[1].pnl)
  return (
    <div className="card scroll" style={{ padding: 12, minHeight: 0 }}>
      <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', marginBottom: 8 }}>{title}</div>
      <table>
        <thead><tr><th style={{ textAlign: 'left' }}>Key</th><th>N</th><th>Win%</th><th>P&L</th></tr></thead>
        <tbody>
          {rows.map(([k, v]) => (
            <tr key={k}>
              <td style={{ textAlign: 'left' }}>{k}</td>
              <td className="mono">{v.n}</td>
              <td className="mono dim">{v.n ? Math.round((v.wins / v.n) * 100) : 0}%</td>
              <td className={`mono ${pnlClass(v.pnl)}`}>{signed(v.pnl)}</td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={4} className="dim">—</td></tr>}
        </tbody>
      </table>
    </div>
  )
}

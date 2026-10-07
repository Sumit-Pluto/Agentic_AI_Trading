import { useState } from 'react'
import { num, type Chain, type Leg, type Snapshot } from '../lib/live'

// Server sends leg IV pre-multiplied to percent (14.0 = 14%).
function pct(iv: number | null | undefined): string {
  return iv == null || !isFinite(iv) ? '—' : `${num(iv, 1)}%`
}

export function ChainView({ snap }: { snap: Snapshot | null }) {
  const chains = snap?.chains ?? {}
  const syms = Object.keys(chains)
  const [sym, setSym] = useState<string>('')
  const active = sym && chains[sym] ? sym : syms[0]
  const ch: Chain | undefined = active ? chains[active] : undefined
  if (!ch) return <div className="dim">waiting for chain…</div>

  const f = ch.features || {}
  const maxOI = Math.max(1, ...ch.rows.flatMap((r) => [r.ce?.oi ?? 0, r.pe?.oi ?? 0]))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, height: '100%', minHeight: 0 }}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        {syms.map((s) => (
          <div key={s} className={`tab ${s === active ? 'active' : ''}`} onClick={() => setSym(s)}>{s}</div>
        ))}
        <div style={{ flex: 1 }} />
        <span className="pill mono">Spot {num(ch.spot, 1)}</span>
        <span className="pill mono">ATM {num(ch.atm, 0)}</span>
        <span className="pill mono">Exp {ch.expiry}</span>
        <span className="pill mono">PCR {num(f.pcr_oi, 2)}</span>
        <span className="pill mono">CE wall {num(f.call_wall, 0)}</span>
        <span className="pill mono">PE wall {num(f.put_wall, 0)}</span>
        <span className="pill mono">Max-pain {num(f.max_pain, 0)}</span>
      </div>

      <div className="card scroll" style={{ minHeight: 0, flex: 1 }}>
        <table>
          <thead><tr>
            <th style={{ textAlign: 'right' }}>CALL OI</th><th>Δ</th><th>IV</th><th>Bid</th><th>Ask</th>
            <th style={{ textAlign: 'center' }}>STRIKE</th>
            <th>Bid</th><th>Ask</th><th>IV</th><th>Δ</th><th style={{ textAlign: 'left' }}>PUT OI</th>
          </tr></thead>
          <tbody>
            {ch.rows.map((r) => {
              const atm = Math.abs(r.strike - (ch.atm ?? 0)) < 1e-6
              return (
                <tr key={r.strike} style={atm ? { background: 'var(--accent-bg)' } : undefined}>
                  <OI leg={r.ce} maxOI={maxOI} side="call" />
                  <td className="mono dim">{num(r.ce?.delta, 2)}</td>
                  <td className="mono">{pct(r.ce?.iv)}</td>
                  <td className="mono">{num(r.ce?.bid, 1)}</td>
                  <td className="mono">{num(r.ce?.ask, 1)}</td>
                  <td className="mono" style={{ textAlign: 'center', fontWeight: 700 }}>{num(r.strike, 0)}</td>
                  <td className="mono">{num(r.pe?.bid, 1)}</td>
                  <td className="mono">{num(r.pe?.ask, 1)}</td>
                  <td className="mono">{pct(r.pe?.iv)}</td>
                  <td className="mono dim">{num(r.pe?.delta, 2)}</td>
                  <OI leg={r.pe} maxOI={maxOI} side="put" />
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function OI({ leg, maxOI, side }: { leg: Leg | null; maxOI: number; side: 'call' | 'put' }) {
  const oi = leg?.oi ?? 0
  const pct = Math.round((oi / maxOI) * 100)
  const col = side === 'call' ? 'var(--red-bg)' : 'var(--green-bg)'
  const align = side === 'call' ? 'right' : 'left'
  return (
    <td style={{ position: 'relative', textAlign: align }}>
      <div style={{ position: 'absolute', top: 3, bottom: 3, [align]: 0, width: `${pct}%`,
        background: col, borderRadius: 3 } as React.CSSProperties} />
      <span className="mono" style={{ position: 'relative' }}>{(oi / 1000).toFixed(0)}k</span>
    </td>
  )
}

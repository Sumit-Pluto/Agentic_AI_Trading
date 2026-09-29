import { inr, num, pnlClass, signed, type Position, type Signal, type Snapshot } from '../lib/live'

export function Cockpit({ snap, equity }: { snap: Snapshot | null; equity: { t: string; equity: number }[] }) {
  const st = snap?.state
  const tiles = [
    { k: 'Day P&L', v: signed(st?.day_pnl), c: pnlClass(st?.day_pnl) },
    { k: 'Realized', v: signed(st?.realized), c: pnlClass(st?.realized) },
    { k: 'Unrealized', v: signed(st?.unrealized), c: pnlClass(st?.unrealized) },
    { k: 'Equity', v: inr(st?.equity), c: '' },
    { k: 'Open Positions', v: String(st?.n_positions ?? 0), c: '' },
    { k: 'Regime', v: st?.regime?.on === false ? 'OFF' : `${Math.round(st?.regime?.avg ?? 0)}`, c: '' },
  ]
  return (
    <div style={{ display: 'grid', gap: 12, gridTemplateColumns: '1fr', height: '100%' }}>
      <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fit,minmax(150px,1fr))' }}>
        {tiles.map((t) => (
          <div key={t.k} className="card" style={{ padding: '12px 14px' }}>
            <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase' }}>{t.k}</div>
            <div className={`mono ${t.c}`} style={{ fontSize: 22, fontWeight: 700, marginTop: 4 }}>{t.v}</div>
          </div>
        ))}
      </div>

      <div style={{ display: 'grid', gap: 12, gridTemplateColumns: '1.3fr 1fr', minHeight: 0 }}>
        <div className="card" style={{ padding: 14, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', marginBottom: 8 }}>Intraday Equity</div>
          <Spark data={equity.map((e) => e.equity)} />
          <div className="dim mono" style={{ marginTop: 6, fontSize: 11 }}>
            {equity.length} marks · {inr(equity[0]?.equity)} → {inr(equity.at(-1)?.equity)}
          </div>
        </div>

        <div className="card scroll" style={{ padding: 14, minHeight: 0 }}>
          <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', marginBottom: 8 }}>Live Signals (last scan)</div>
          {(snap?.signals?.length ?? 0) === 0
            ? <div className="dim">no candidate cleared the threshold this bar</div>
            : snap!.signals.map((s, i) => <SignalRow key={i} s={s} />)}
        </div>
      </div>

      <div className="card scroll" style={{ minHeight: 0, maxHeight: 240 }}>
        <table>
          <thead><tr><th>Position</th><th>Side</th><th>Lots</th><th>Entry</th><th>Mark</th><th>P&L</th><th>Stop</th><th>Age</th><th>Strategy</th></tr></thead>
          <tbody>
            {(snap?.positions ?? []).map((p) => <PosRow key={p.symbol} p={p} />)}
            {(snap?.positions?.length ?? 0) === 0 && <tr><td colSpan={9} className="dim">no open positions</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SignalRow({ s }: { s: Signal }) {
  const buy = s.direction === 'BUY'
  const inst = s.instrument as { tsym?: string; strike?: number; right?: string } | null
  return (
    <div className="flash" style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 0', borderBottom: '1px solid var(--line)' }}>
      <span className="pill" style={{ color: buy ? 'var(--green)' : 'var(--red)', borderColor: buy ? 'var(--green)' : 'var(--red)' }}>{s.direction}</span>
      <b>{s.symbol}</b>
      <span className="mono dim">{inst?.tsym ?? `${inst?.strike ?? ''}${inst?.right ?? ''}`}</span>
      <div style={{ flex: 1 }} />
      <span className="mono">B {num(s.score_buy, 0)} / S {num(s.score_sell, 0)}</span>
    </div>
  )
}

function PosRow({ p }: { p: Position }) {
  return (
    <tr>
      <td className="mono">{p.symbol}</td>
      <td><span className="pill" style={{ color: p.right === 'CE' ? 'var(--green)' : 'var(--red)' }}>{p.side} {p.right}</span></td>
      <td className="mono">{p.lots}</td>
      <td className="mono">{num(p.entry_px)}</td>
      <td className="mono">{num(p.mark)}</td>
      <td className={`mono ${pnlClass(p.pnl)}`}>{signed(p.pnl)}</td>
      <td className="mono dim">{num(p.stop, 0)}</td>
      <td className="mono dim">{p.age_bars}</td>
      <td className="dim">{p.strategy}</td>
    </tr>
  )
}

function Spark({ data }: { data: number[] }) {
  const w = 640, h = 120, pad = 6
  if (data.length < 2) return <div className="dim" style={{ height: h }}>collecting…</div>
  const min = Math.min(...data), max = Math.max(...data), span = max - min || 1
  const pts = data.map((v, i) => {
    const x = pad + (i / (data.length - 1)) * (w - 2 * pad)
    const y = h - pad - ((v - min) / span) * (h - 2 * pad)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const up = data.at(-1)! >= data[0]
  const col = up ? 'var(--green)' : 'var(--red)'
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: '100%', height: h }}>
      <polyline points={pts} fill="none" stroke={col} strokeWidth={1.8} />
      <line x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} stroke="var(--line)" strokeWidth={0.5} />
    </svg>
  )
}

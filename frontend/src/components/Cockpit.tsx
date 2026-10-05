import { useEffect, useState } from 'react'
import { inr, num, pnlClass, signed, type Position, type Signal, type Snapshot } from '../lib/live'

export function Cockpit({ snap, equity }: { snap: Snapshot | null; equity: { t: string; equity: number }[] }) {
  const st = snap?.state
  const b = snap?.budget
  const funds = snap?.funds as { cash?: number | string; margin_used?: number | string } | null | undefined
  const capColor = b?.cap_state === 'EXHAUSTED' ? 'neg' : b?.cap_state === 'SOFT_CAP' ? '' : 'pos'
  const tiles = [
    { k: 'Day P&L', v: signed(st?.day_pnl), c: pnlClass(st?.day_pnl) },
    { k: 'Realized', v: signed(st?.realized), c: pnlClass(st?.realized) },
    { k: 'Unrealized', v: signed(st?.unrealized), c: pnlClass(st?.unrealized) },
    { k: 'Equity', v: inr(st?.equity), c: '' },
    { k: 'Open Positions', v: String(st?.n_positions ?? 0), c: '' },
    { k: 'Regime', v: st?.regime?.on === false ? 'OFF' : `${Math.round(st?.regime?.avg ?? 0)}`, c: '' },
  ]
  const budgetTiles = [
    { k: 'Budget', v: inr(b?.total), c: '' },
    { k: 'Deployed', v: inr(b?.deployed), c: '' },
    { k: 'Utilisation', v: b?.utilisation_pct != null ? `${num(b.utilisation_pct, 1)}%` : '—', c: capColor },
    { k: 'Cap State', v: (b?.cap_state || 'OK').replace('_', ' '), c: capColor },
    ...(funds ? [
      { k: 'Account Cash', v: inr(Number(funds.cash) || 0), c: '' },
      { k: 'Margin Used', v: inr(Number(funds.margin_used) || 0), c: '' },
    ] : []),
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
      <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fit,minmax(130px,1fr))' }}>
        {budgetTiles.map((t) => (
          <div key={t.k} className="card" style={{ padding: '10px 12px' }}>
            <div className="faint" style={{ fontSize: 10, textTransform: 'uppercase' }}>{t.k}</div>
            <div className={`mono ${t.c}`} style={{ fontSize: 16, fontWeight: 700, marginTop: 3 }}>{t.v}</div>
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

function useNow(step = 500) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), step)
    return () => clearInterval(id)
  }, [step])
  return now
}

function hhmmss(iso?: string): string {
  if (!iso) return '—'
  const m = /T(\d{2}:\d{2}:\d{2})/.exec(iso)
  return m ? m[1] : iso.slice(0, 8)
}

function ageStr(ts: string | undefined, now: number): string {
  if (!ts) return '—'
  const ms = now - new Date(ts).getTime()
  if (!isFinite(ms) || ms < 0) return '—'
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s ago`
  return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s ago`
}

function SignalRow({ s }: { s: Signal }) {
  const buy = s.direction === 'BUY'
  const [open, setOpen] = useState(false)
  const now = useNow()
  const inst = s.instrument
  const wp = inst?.win_prob
  const fams = Object.entries(s.family_scores ?? {})
  return (
    <div className="flash" style={{ padding: '6px 0', borderBottom: '1px solid var(--line)' }}>
      <div onClick={() => setOpen(!open)}
        style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer' }}
        title="click for the full signal card">
        <span className="dim mono">{open ? '▾' : '▸'}</span>
        <span className="pill" style={{ color: buy ? 'var(--green)' : 'var(--red)', borderColor: buy ? 'var(--green)' : 'var(--red)' }}>{s.direction}</span>
        <b>{s.symbol}</b>
        <span className="mono dim">{inst?.tsym ?? `${inst?.strike ?? ''}${inst?.right ?? ''}`}</span>
        <div style={{ flex: 1 }} />
        <span className="mono dim" title={`fired at ${s.ts} IST`}>{hhmmss(s.ts)} · {ageStr(s.ts, now)}</span>
        {s.scan_ms != null && <span className="mono dim" title="agent-scan compute time">scan {num(s.scan_ms, 0)}ms</span>}
        {wp != null && <span className="pill mono" title="trained-model win probability"
          style={{ color: wp >= 0.5 ? 'var(--green)' : 'var(--amber)' }}>P {Math.round(wp * 100)}%</span>}
        <span className="mono">B {num(s.score_buy, 0)} / S {num(s.score_sell, 0)}</span>
      </div>
      {open && (
        <div style={{ marginTop: 8, display: 'grid', gap: 10, paddingLeft: 22 }}>
          <div className="mono" style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12 }}>
            <span title="fired-side composite">score <b>{num(s.composite ?? (buy ? s.score_buy : s.score_sell), 1)}</b></span>
            <span title="|buy − sell| anti-ambiguity margin">margin {num(s.margin, 1)}</span>
            <span title="agents scored (of 25)">agents {s.n_scored ?? '—'}</span>
            <span title="decision layer version">brain {s.brain_version ?? '—'}</span>
            {(s.vetoes?.length ?? 0) > 0
              ? <span style={{ color: 'var(--red)' }}>vetoes: {s.vetoes!.join('; ')}</span>
              : <span className="dim">no vetoes</span>}
          </div>
          {fams.length > 0 && (
            <div>
              <div className="faint" style={{ fontSize: 10, textTransform: 'uppercase', marginBottom: 4 }}>
                Family scores ({buy ? 'BUY' : 'SELL'} side)
              </div>
              {fams.map(([f, v]) => (
                <div key={f} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                  <span className="mono dim" style={{ width: 14 }}>{f}</span>
                  <div style={{ flex: 1, height: 6, background: 'var(--line)', borderRadius: 3 }}>
                    <div style={{ width: `${Math.max(0, Math.min(100, v))}%`, height: '100%', borderRadius: 3,
                      background: buy ? 'var(--green)' : 'var(--red)' }} />
                  </div>
                  <span className="mono" style={{ width: 36, textAlign: 'right' }}>{num(v, 0)}</span>
                </div>
              ))}
            </div>
          )}
          {inst && (
            <div>
              <div className="faint" style={{ fontSize: 10, textTransform: 'uppercase', marginBottom: 4 }}>Instrument</div>
              <div className="mono" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(150px,1fr))', gap: '2px 12px', fontSize: 12 }}>
                <span className="dim">vehicle <b style={{ color: 'var(--fg)' }}>{inst.kind ?? 'OPT'}</b></span>
                <span className="dim">strike <b style={{ color: 'var(--fg)' }}>{inst.strike ?? '—'} {inst.right ?? ''}</b></span>
                <span className="dim">expiry <b style={{ color: 'var(--fg)' }}>{inst.expiry ?? '—'}</b></span>
                <span className="dim">lot <b style={{ color: 'var(--fg)' }}>{inst.lot_size ?? '—'}</b></span>
                <span className="dim">entry <b style={{ color: 'var(--fg)' }}>{num(inst.entry_prem ?? inst.ask)}</b></span>
                <span className="dim">bid/ask <b style={{ color: 'var(--fg)' }}>{num(inst.bid)}/{num(inst.ask)}</b></span>
                <span className="dim">delta <b style={{ color: 'var(--fg)' }}>{num(inst.delta)}</b></span>
                <span className="dim">IV <b style={{ color: 'var(--fg)' }}>{inst.iv != null ? `${num(inst.iv * 100, 1)}%` : '—'}</b></span>
                <span className="dim">route <b style={{ color: 'var(--fg)' }}>{inst.exch ?? '—'}:{inst.token || '—'}</b></span>
                <span className="dim">strategy <b style={{ color: 'var(--fg)' }}>{inst.strategy ?? 'default'}</b></span>
              </div>
              {inst.selector_reason && <div className="dim mono" style={{ fontSize: 11, marginTop: 2 }}>why {inst.kind ?? 'OPT'}: {inst.selector_reason}</div>}
            </div>
          )}
          {s.regime && (
            <div className="mono dim" style={{ fontSize: 12 }}>
              regime {s.regime.on === false ? <b style={{ color: 'var(--red)' }}>OFF</b> : <b style={{ color: 'var(--green)' }}>ON</b>}
              {s.regime.avg != null && <> · avg {num(s.regime.avg, 0)}</>}
              {s.regime.scalar != null && <> · size ×{num(s.regime.scalar, 2)}</>}
              {s.regime.detail && <> · {s.regime.detail}</>}
              {(s.regime.vetoes?.length ?? 0) > 0 && <> · vetoes: {s.regime.vetoes!.join('; ')}</>}
            </div>
          )}
          {(s.agents?.length ?? 0) > 0 && (
            <div style={{ maxHeight: 180, overflowY: 'auto' }}>
              <table>
                <thead><tr><th>Agent</th><th>Fam</th><th>B</th><th>S</th><th>N/A · veto · detail</th></tr></thead>
                <tbody>
                  {s.agents!.map((a, i) => (
                    <tr key={i}>
                      <td className="mono">{a.agent}</td>
                      <td className="mono dim">{a.family}</td>
                      <td className="mono">{a.buy ?? '—'}</td>
                      <td className="mono">{a.sell ?? '—'}</td>
                      <td className="dim" style={{ fontSize: 11 }}>
                        {[a.na && `N/A: ${a.na}`, a.veto && `VETO: ${a.veto}`,
                          a.veto_long && `no-long: ${a.veto_long}`, a.veto_short && `no-short: ${a.veto_short}`,
                          a.detail].filter(Boolean).join(' · ')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
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

import { apiPost, inr, pnlClass, signed, type Snapshot } from '../lib/live'

export function Header({ snap, connected }: { snap: Snapshot | null; connected: boolean }) {
  const st = snap?.state
  const regime = st?.regime
  const paused = !!st?.paused
  const halted = !!st?.halted

  async function togglePause() {
    await apiPost('/api/pause', { paused: !paused })
  }
  async function goLive() {
    const r = await apiPost<{ error?: string; mode?: string }>('/api/mode', { mode: 'live' })
    alert(r.error || `mode → ${r.mode}`)
  }

  const regimeOn = regime?.on !== false && !(regime?.vetoes && regime.vetoes.length)
  return (
    <header className="card" style={{ borderRadius: 0, borderLeft: 0, borderRight: 0, borderTop: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, padding: '10px 16px', flexWrap: 'wrap' }}>
        <div style={{ fontWeight: 800, letterSpacing: 0.5 }}>◆ INTRADAY&nbsp;AGENTIC</div>
        <span className="pill mono">{new Date(st?.now || Date.now()).toISOString().slice(11, 16)} IST</span>
        <span className="pill" title="engine data source · broker mode">
          {(st?.mode || 'sim').toUpperCase()} · PAPER
        </span>
        <span className="pill" style={{ borderColor: regimeOn ? 'var(--green)' : 'var(--red)',
          color: regimeOn ? 'var(--green)' : 'var(--red)' }}>
          REGIME {regimeOn ? 'ON' : 'OFF'}{regime?.avg != null ? ` · ${Math.round(regime.avg)}` : ''}
        </span>
        <span className="pill mono">VIX {st?.vix ?? '—'}</span>
        {halted && <span className="pill blink" style={{ color: 'var(--red)', borderColor: 'var(--red)' }}>DAILY-LOSS HALT</span>}

        <div style={{ flex: 1 }} />

        <Stat label="Day P&L" value={signed(st?.day_pnl)} cls={pnlClass(st?.day_pnl)} big />
        <Stat label="Equity" value={inr(st?.equity)} />
        <Stat label="Open" value={String(st?.n_positions ?? 0)} />

        <button className="tab" onClick={togglePause}
          style={{ borderColor: paused ? 'var(--amber)' : 'var(--line)', color: paused ? 'var(--amber)' : undefined }}>
          {paused ? '► Resume' : '❚❚ Pause'}
        </button>
        <button className="tab" onClick={goLive} style={{ borderColor: 'var(--line)' }}>Go LIVE</button>
        <span title={connected ? 'live feed connected' : 'reconnecting…'}
          style={{ width: 9, height: 9, borderRadius: 9, background: connected ? 'var(--green)' : 'var(--amber)' }}
          className={connected ? '' : 'blink'} />
      </div>
    </header>
  )
}

function Stat({ label, value, cls, big }: { label: string; value: string; cls?: string; big?: boolean }) {
  return (
    <div style={{ textAlign: 'right', lineHeight: 1.1 }}>
      <div className="faint" style={{ fontSize: 10, textTransform: 'uppercase' }}>{label}</div>
      <div className={`mono ${cls || ''}`} style={{ fontSize: big ? 20 : 14, fontWeight: 700 }}>{value}</div>
    </div>
  )
}

import { apiPost, hhmm, inr, pnlClass, signed, type Snapshot } from '../lib/live'

export function Header({ snap, connected }: { snap: Snapshot | null; connected: boolean }) {
  const st = snap?.state
  const regime = st?.regime
  const paused = !!st?.paused
  const halted = !!st?.halted
  const live = (st?.trading_mode || 'paper').toLowerCase() === 'live'
  const cap = st?.cap_state || 'OK'

  async function togglePause() {
    await apiPost('/api/pause', { paused: !paused })
  }
  async function toggleMode() {
    if (live) { await apiPost('/api/mode', { mode: 'paper' }); return }
    const typed = window.prompt(
      '⚠ REAL-MONEY trading. This will place live orders on the shared account.\n\nType LIVE to confirm:')
    if (typed !== 'LIVE') return
    const r = await apiPost<{ error?: string; mode?: string }>('/api/mode', { mode: 'live', confirm: 'LIVE' })
    if (r.error) alert(r.error)
  }
  async function kill() {
    if (!window.confirm('KILL SWITCH — halt trading and flatten ALL open positions now?')) return
    await apiPost('/api/kill', {})
  }

  const regimeOn = regime?.on !== false && !(regime?.vetoes && regime.vetoes.length)
  const capColor = cap === 'EXHAUSTED' ? 'var(--red)' : cap === 'SOFT_CAP' ? 'var(--amber)' : 'var(--green)'
  return (
    <header className="card" style={{ borderRadius: 0, borderLeft: 0, borderRight: 0, borderTop: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '10px 16px', flexWrap: 'wrap' }}>
        <div style={{ fontWeight: 800, letterSpacing: 0.5 }}>◆ INTRADAY&nbsp;AGENTIC</div>
        <span className="pill mono">{hhmm(st?.now)} IST</span>
        <span className="pill" title={`broker: ${st?.broker || '—'}`}
          style={{ borderColor: live ? 'var(--red)' : 'var(--green)',
            color: live ? 'var(--red)' : 'var(--green)', fontWeight: 800 }}>
          {live ? '● REAL MONEY' : '🧪 PAPER'}
        </span>
        <span className="pill" title={`data source: ${st?.data_source || '—'}`}
          style={{ borderColor: st?.live_data ? 'var(--green)' : 'var(--amber)',
            color: st?.live_data ? 'var(--green)' : 'var(--amber)' }}>
          {st?.live_data ? 'LIVE DATA' : 'SIM DATA'}
        </span>
        <span className="pill" style={{ borderColor: regimeOn ? 'var(--green)' : 'var(--red)',
          color: regimeOn ? 'var(--green)' : 'var(--red)' }}>
          REGIME {regimeOn ? 'ON' : 'OFF'}{regime?.avg != null ? ` · ${Math.round(regime.avg)}` : ''}
        </span>
        <span className="pill" style={{ borderColor: capColor, color: capColor }}>{cap.replace('_', ' ')}</span>
        <span className="pill mono">VIX {st?.vix ?? '—'}</span>
        {halted && <span className="pill blink" style={{ color: 'var(--red)', borderColor: 'var(--red)' }}>HALTED</span>}

        <div style={{ flex: 1 }} />

        <Stat label="Day P&L" value={signed(st?.day_pnl)} cls={pnlClass(st?.day_pnl)} big />
        <Stat label="Equity" value={inr(st?.equity)} />
        <Stat label="Open" value={String(st?.n_positions ?? 0)} />

        <button className="tab" onClick={togglePause}
          style={{ borderColor: paused ? 'var(--green)' : 'var(--amber)',
            color: paused ? 'var(--green)' : 'var(--amber)', fontWeight: 700 }}>
          {paused ? '► Start' : '❚❚ Stop'}
        </button>
        <button className="tab" onClick={toggleMode}
          style={{ borderColor: live ? 'var(--green)' : 'var(--red)', color: live ? 'var(--green)' : 'var(--red)' }}>
          {live ? 'Go PAPER' : 'Go LIVE'}
        </button>
        <button className="tab" onClick={kill}
          style={{ borderColor: 'var(--red)', color: 'var(--red)', fontWeight: 700 }}>Kill</button>
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

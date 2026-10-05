import { useEffect, useState } from 'react'
import { apiGet, apiPost } from '../lib/live'

const KNOBS: { key: string; label: string; group: string }[] = [
  { key: 'total_budget', label: 'Total budget (₹)', group: 'Capital' },
  { key: 'soft_cap_pct', label: 'Soft cap (%)', group: 'Capital' },
  { key: 'hard_cap_pct', label: 'Hard cap (%)', group: 'Capital' },
  { key: 'global_sl_pct', label: 'Global MTM SL (%)', group: 'Capital' },
  { key: 'margin_safety_factor', label: 'Margin safety ×', group: 'Capital' },
  { key: 'risk_per_trade_pct', label: 'Risk / trade (%)', group: 'Money & Risk' },
  { key: 'max_daily_loss_rupees', label: 'Max daily loss (₹)', group: 'Money & Risk' },
  { key: 'max_positions', label: 'Max open positions', group: 'Money & Risk' },
  { key: 'max_lots_per_symbol', label: 'Max lots / symbol', group: 'Money & Risk' },
  { key: 'max_prem_loss_pct', label: 'Premium stop (%)', group: 'Money & Risk' },
  { key: 'roundtrip_cost_per_lot', label: 'Costs / lot RT (₹)', group: 'Money & Risk' },
  { key: 'score_threshold', label: 'Signal threshold', group: 'Decision' },
  { key: 'regime_min_avg', label: 'Regime min avg', group: 'Decision' },
  { key: 'score_margin', label: 'Direction margin', group: 'Decision' },
  { key: 'atr_stop_mult', label: 'ATR stop ×', group: 'Decision' },
  { key: 'model_filter_min_prob', label: 'Model min P(win)', group: 'Decision' },
  { key: 'target_r_1', label: 'Target 1 (R)', group: 'Exits' },
  { key: 'target_r_2', label: 'Target 2 (R)', group: 'Exits' },
  { key: 'trail_atr_mult', label: 'Trail ATR ×', group: 'Exits' },
  { key: 'square_off_time', label: 'Square-off time', group: 'Session' },
  { key: 'no_new_entries_after', label: 'No new entries after', group: 'Session' },
  { key: 'demo_step_seconds', label: 'Scan step (s)', group: 'Session' },
  { key: 'scan_every_seconds', label: 'Live scan cadence (s)', group: 'Session' },
  { key: 'live_confirm_timeout_s', label: 'Live fill wait (s)', group: 'Session' },
]

export function Settings() {
  const [cfg, setCfg] = useState<Record<string, unknown>>({})
  const [saved, setSaved] = useState('')
  useEffect(() => { apiGet<Record<string, unknown>>('/api/config').then(setCfg).catch(() => {}) }, [])

  async function save() {
    const body: Record<string, unknown> = {}
    for (const { key } of KNOBS) {
      const v = cfg[key]
      body[key] = typeof v === 'string' && key !== 'square_off_time' && !isNaN(Number(v)) ? Number(v) : v
    }
    await apiPost('/api/config', body)
    setSaved('saved ✓'); setTimeout(() => setSaved(''), 1500)
  }

  const groups = Array.from(new Set(KNOBS.map((k) => k.group)))
  return (
    <div className="scroll" style={{ minHeight: 0, display: 'grid', gap: 12,
      gridTemplateColumns: 'repeat(auto-fill,minmax(280px,1fr))', alignContent: 'start' }}>
      {groups.map((g) => (
        <div key={g} className="card" style={{ padding: 14 }}>
          <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', marginBottom: 10 }}>{g}</div>
          {KNOBS.filter((k) => k.group === g).map(({ key, label }) => (
            <label key={key} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 10 }}>
              <span className="dim">{label}</span>
              <input className="mono" value={String(cfg[key] ?? '')}
                onChange={(e) => setCfg({ ...cfg, [key]: e.target.value })}
                style={{ width: 110, background: 'var(--bg)', border: '1px solid var(--line)',
                  color: 'var(--fg)', borderRadius: 6, padding: '4px 8px', textAlign: 'right' }} />
            </label>
          ))}
        </div>
      ))}
      <div className="card" style={{ padding: 14, display: 'flex', alignItems: 'center', gap: 12 }}>
        <button className="tab active" onClick={save}>Save config</button>
        <span className="pos">{saved}</span>
        <div className="faint" style={{ fontSize: 11 }}>Applies live to the running engine (paper/sim). Kill-switch = Pause in the header.</div>
      </div>
    </div>
  )
}

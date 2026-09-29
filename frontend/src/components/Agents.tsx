import { useState } from 'react'
import { type AgentRow, type Snapshot } from '../lib/live'

const FAMILIES: Record<string, string> = {
  R: 'Regime', S: 'Setup', F: 'Flow / OI', V: 'Volatility', M: 'Momentum', C: 'Catalyst',
}

export function AgentsView({ snap }: { snap: Snapshot | null }) {
  const rows = snap?.agent_rows ?? []
  const syms = Array.from(new Set(rows.map((r) => r.symbol)))
  const [sym, setSym] = useState<string>('')
  const active = sym && syms.includes(sym) ? sym : syms[0]
  const regime = snap?.state?.regime

  // regime rows are index-level (symbol = index); the rest are per candidate
  const forSym = rows.filter((r) => r.symbol === active)
  const byFam: Record<string, AgentRow[]> = {}
  for (const r of forSym) (byFam[r.family] ||= []).push(r)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, height: '100%', minHeight: 0 }}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
        {syms.map((s) => <div key={s} className={`tab ${s === active ? 'active' : ''}`} onClick={() => setSym(s)}>{s}</div>)}
        <div style={{ flex: 1 }} />
        <span className="pill" style={{ borderColor: regime?.on === false ? 'var(--red)' : 'var(--green)' }}>
          {regime?.detail || 'regime'}
        </span>
        {(regime?.vetoes ?? []).map((v, i) => <span key={i} className="pill" style={{ color: 'var(--red)' }}>{v}</span>)}
      </div>

      <div className="scroll" style={{ minHeight: 0, flex: 1, display: 'grid', gap: 12,
        gridTemplateColumns: 'repeat(auto-fill,minmax(320px,1fr))', alignContent: 'start' }}>
        {Object.keys(FAMILIES).filter((f) => byFam[f]?.length).map((fam) => (
          <div key={fam} className="card" style={{ padding: 12 }}>
            <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', marginBottom: 8 }}>
              {fam} · {FAMILIES[fam]}
            </div>
            {byFam[fam].map((r) => <AgentBar key={r.agent} r={r} />)}
          </div>
        ))}
      </div>
    </div>
  )
}

function AgentBar({ r }: { r: AgentRow }) {
  const veto = r.veto || r.veto_long || r.veto_short
  const na = r.na
  const buy = r.score_buy ?? 50
  return (
    <div style={{ marginBottom: 8 }} title={r.detail}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
        <span>{r.agent.replace(/_/g, ' ')}</span>
        <span className="mono dim">
          {veto ? '⛔ VETO' : na ? 'n/a' : Math.round(buy)}
        </span>
      </div>
      <div style={{ height: 6, background: 'var(--bg3)', borderRadius: 4, marginTop: 3, overflow: 'hidden' }}>
        {!veto && !na && (
          <div style={{ height: '100%', width: `${buy}%`,
            background: buy >= 55 ? 'var(--green)' : buy <= 45 ? 'var(--red)' : 'var(--faint)' }} />
        )}
        {veto && <div style={{ height: '100%', width: '100%', background: 'var(--red-bg)' }} />}
      </div>
      {(veto || (r.detail && !na)) && <div className="faint" style={{ fontSize: 10, marginTop: 2 }}>{veto || r.detail}</div>}
    </div>
  )
}

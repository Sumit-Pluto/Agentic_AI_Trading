import { useState } from 'react'
import { hhmm, type Snapshot } from '../lib/live'

const STAGE_COLOR: Record<string, string> = {
  scan: 'var(--green)',
  entry: 'var(--amber)',
  exits: 'var(--amber)',
  halt: 'var(--red)',
}

export function ActivityView({ snap }: { snap: Snapshot | null }) {
  const events = [...(snap?.activity ?? [])].reverse()
  const scan = snap?.scan ?? []
  const [stage, setStage] = useState<string>('')
  const stages = Array.from(new Set(events.map((e) => e.stage)))
  const shown = stage ? events.filter((e) => e.stage === stage) : events
  const withBars = scan.filter((s) => s.bars > 0).length
  const withChain = scan.filter((s) => s.chain).length

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, height: '100%', minHeight: 0 }}>
      <div className="card" style={{ padding: 12 }}>
        <div className="faint" style={{ fontSize: 11, textTransform: 'uppercase', marginBottom: 8 }}>
          Last scan · {scan.length} symbols ({withBars} bars, {withChain} chains)
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {scan.map((s) => (
            <span key={s.symbol} className="pill mono" title={
              `${s.symbol} · ${s.bars} bars · chain ${s.chain ? 'ok' : 'missing'}`}>
              <span style={{ color: s.bars > 0 ? 'var(--green)' : 'var(--red)' }}>●</span>
              &nbsp;{s.symbol}&nbsp;
              <span style={{ color: s.chain ? 'var(--green)' : 'var(--amber)' }}>◆</span>
            </span>
          ))}
          {!scan.length && <span className="faint">waiting for first scan…</span>}
        </div>
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <div className={`tab ${stage === '' ? 'active' : ''}`} onClick={() => setStage('')}>all</div>
        {stages.map((s) => (
          <div key={s} className={`tab ${s === stage ? 'active' : ''}`} onClick={() => setStage(s)}>{s}</div>
        ))}
        <div style={{ flex: 1 }} />
        <span className="faint mono" style={{ fontSize: 11 }}>{shown.length} events</span>
      </div>

      <div className="scroll" style={{ minHeight: 0, flex: 1 }}>
        {shown.map((e, i) => (
          <div key={`${e.ts}-${i}`} className="mono" style={{ display: 'flex', gap: 10, fontSize: 12,
            padding: '5px 4px', borderBottom: '1px solid var(--line)' }}>
            <span className="faint" style={{ flexShrink: 0 }}>{hhmm(e.ts)}</span>
            <span style={{ flexShrink: 0, fontWeight: 700, minWidth: 52,
              color: STAGE_COLOR[e.stage] || 'var(--fg)' }}>{e.stage}</span>
            <span>{e.msg}</span>
          </div>
        ))}
        {!shown.length && <div className="faint" style={{ padding: 12 }}>no events yet — start the engine to see background processing here.</div>}
      </div>
    </div>
  )
}

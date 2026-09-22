import { useEffect, useState } from 'react'
import { jget, wlAdd } from '../lib/api'
import { fmt } from '../lib/format'
import { Badge } from '../components/ui'

interface AgentSignal {
  fired_at: string
  symbol: string
  segment: string
  direction: string
  price: number
  score: number
  opp_score: number
  margin: number
  families?: Record<string, number>
}

const FAM_KEYS = ['smc', 'snr', 'volatility', 'volume', 'macro']

export function AgentsView({ visible }: { visible: boolean }) {
  const [rows, setRows] = useState<AgentSignal[]>([])
  const [meta, setMeta] = useState('')
  const [params, setParams] = useState('')

  async function refresh() {
    try {
      const j = await jget<{ count: number; last_scan: string; params: Record<string, unknown>; signals: AgentSignal[] }>('/api/agent-signals')
      setMeta(`${j.count || 0} signals · ${j.last_scan || 'not scanned yet'}`)
      const p = j.params || {}
      setParams(
        `bar ≥ ${p.min_score ?? '?'} · edge ≥ ${p.margin ?? '?'} · cooldown ${p.cooldown_min ?? '?'}m · paper autotrade ${
          p.autotrade ? 'ON' : 'off'
        } · live ${p.live_enabled ? 'ENABLED' : 'blocked'}`,
      )
      setRows(j.signals || [])
    } catch (e) {
      setMeta('error: ' + (e as Error).message)
    }
  }

  useEffect(() => {
    if (visible) refresh()
  }, [visible])

  const fam = (f?: Record<string, number>) => FAM_KEYS.map((k) => (f && f[k] != null ? Math.round(f[k]) : '–')).join('/')

  return (
    <div className={`flex-1 min-h-0 overflow-y-auto px-5 py-4 ${visible ? '' : 'hidden'}`}>
      <div className="font-bold mb-2.5 flex items-center gap-2.5">
        Agents — tree-primary signals <span className="text-dim font-normal">(no indicator: score ≥ bar + edge over the opposite side, rising-edge)</span>
        <span className="text-dim font-normal">{meta}</span>
        <button onClick={refresh} className="text-xs px-2 py-0.5 bg-bg3 border border-line rounded hover:border-accent hover:text-accent">
          ↻
        </button>
      </div>
      <div className="text-dim text-[11px] mb-2">{params}</div>
      <div className="overflow-x-auto">
        <table className="border-collapse w-full text-xs">
          <thead>
            <tr>
              {['Fired', 'Symbol', 'Seg', 'Dir', 'Price', 'Score', 'Opp', 'Edge', 'Families (smc/snr/vol/volume/macro)', ''].map((h) => (
                <th key={h} className="text-left px-2.5 py-1 border-b border-line text-dim font-semibold whitespace-nowrap">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length ? (
              rows.map((s, i) => (
                <tr key={i} className="hover:bg-black/5">
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{(s.fired_at || '').slice(11, 16)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">
                    <b>{s.symbol}</b>
                  </td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{s.segment}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">
                    <Badge direction={s.direction} />
                  </td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{fmt(s.price, 2)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">
                    <b>{fmt(s.score, 1)}</b>
                  </td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap text-dim">{fmt(s.opp_score, 1)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap text-green">+{fmt(s.margin, 1)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap text-dim">{fam(s.families)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">
                    <button
                      onClick={() => wlAdd({ symbol: s.symbol, strategy: 'agents', direction: s.direction, segment: s.segment })}
                      className="text-[11px] px-2 py-0.5 bg-bg3 border border-line rounded hover:border-accent hover:text-accent"
                    >
                      + watch
                    </button>
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={10} className="px-2.5 py-1 text-dim">
                  No agent-primary signals yet — the tree fires only on a fresh score crossing with real directional edge.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="text-dim text-[11px] mt-2.5">
        Fired signals are paper-traded as strategy=<b>agents</b> (see P&amp;L / positions) to build the outcome record. In LIVE mode they are recorded but
        never traded until AGENTS_PRIMARY_LIVE=1.
      </div>
    </div>
  )
}

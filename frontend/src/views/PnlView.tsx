import { useEffect, useRef, useState } from 'react'
import { AgentTreeView } from '../components/AgentTree'
import { useChart } from '../hooks/useChart'
import { Badge, Chip, Tile } from '../components/ui'
import { jget } from '../lib/api'
import { fmt, hhmmss, numOr, parseBarTime, signed } from '../lib/format'
import type { TreeNode } from '../lib/types'

function pnlCls(v: unknown): string {
  const n = numOr(v)
  return n === null ? '' : n > 0 ? 'text-green' : n < 0 ? 'text-red' : ''
}
function pnlWhen(iso?: string) {
  if (!iso) return '–'
  const s = String(iso)
  return s.length >= 16 ? s.slice(5, 16).replace('T', ' ') : s
}
function reasonTone(t: any): 'ok' | 'bad' | 'na' | 'openpos' {
  if (t.state === 'OPEN' || t.state === 'PARTIAL') return 'openpos'
  const r = String(t.exit_reason || '?')
  if (r === 'stop') return 'bad'
  if (r === 'target1' || r === 'target2') return 'ok'
  return 'na'
}
function typeTag(t: any) {
  const sim = t.source === 'sim'
  const live = !!(t.live || t.real)
  const label = sim ? 'SIM' : live ? 'LIVE ₹' : 'PAPER'
  const tone = sim ? 'na' : live ? 'bad' : 'ok'
  const title = sim ? 'Simulated — replayed last-day walk, not real money' : live ? 'Live real-money Shoonya order' : 'Paper trade (no real order)'
  return { label, tone: tone as 'ok' | 'bad' | 'na', title }
}
function tlClass(s: string) {
  if (/^ENTRY /.test(s)) return 'before:bg-accent'
  if (/^EXIT /.test(s)) return 'before:bg-amber'
  if (/^RESULT: \+0\.00/.test(s)) return ''
  if (/^RESULT: \+/.test(s)) return 'text-green before:bg-green'
  if (/^RESULT: /.test(s)) return 'text-red before:bg-red'
  if (/^POSITION /.test(s)) return 'before:bg-accent'
  return ''
}

export function PnlView({ visible }: { visible: boolean }) {
  const [account, setAccount] = useState<any>(null)
  const [trades, setTrades] = useState<any[]>([])
  const [genAt, setGenAt] = useState('')
  const [selId, setSelId] = useState<string | null>(null)

  async function refresh() {
    try {
      const d = await jget<any>('/api/pnl')
      setAccount(d.account || {})
      setTrades(Array.isArray(d.trades) ? d.trades : [])
      setGenAt(d.generated_at ? '· as of ' + hhmmss(d.generated_at) : '')
    } catch {
      /* best-effort */
    }
  }

  useEffect(() => {
    if (visible) refresh()
  }, [visible])

  useEffect(() => {
    const id = setInterval(() => {
      if (visible && document.visibilityState !== 'hidden') refresh()
    }, 30000)
    return () => clearInterval(id)
  }, [visible])

  const a = account || {}
  const pf = numOr(a.profit_factor)

  return (
    <div className={`flex-1 min-h-0 overflow-y-auto px-4.5 py-3.5 pb-10 ${visible ? '' : 'hidden'}`}>
      <div className="text-[10px] uppercase tracking-wider text-dim mt-0 mb-2">
        Account — live paper, ₹ per share <span className="normal-case tracking-normal">(sims excluded)</span>
      </div>
      <div className="flex flex-wrap gap-2.5">
        <Tile label="Realized total ₹/sh" value={signed(a.realized_total_ps)} valueClass={pnlCls(a.realized_total_ps)} />
        <Tile label="Realized today ₹/sh" value={signed(a.realized_today_ps)} valueClass={pnlCls(a.realized_today_ps)} />
        <Tile label="Unrealized ₹/sh" value={signed(a.unrealized_ps)} valueClass={pnlCls(a.unrealized_ps)} sub="open positions" />
        <Tile label="Open" value={numOr(a.open_count) === null ? '–' : String(a.open_count)} />
        <Tile label="Trades" value={numOr(a.trades_total) === null ? '–' : String(a.trades_total)} sub={`${numOr(a.wins) || 0}W / ${numOr(a.losses) || 0}L`} />
        <Tile label="Win rate" value={numOr(a.win_rate) === null ? '–' : Number(a.win_rate).toFixed(1) + '%'} />
        <Tile label="Avg win" value={signed(a.avg_win_ps)} valueClass={pnlCls(a.avg_win_ps)} />
        <Tile label="Avg loss" value={signed(a.avg_loss_ps)} valueClass={pnlCls(a.avg_loss_ps)} />
        <Tile label="Profit factor" value={pf === null ? '–' : pf.toFixed(2)} valueClass={pf === null ? '' : pnlCls(pf - 1)} />
        <Tile label="Best" value={signed(a.best_ps)} valueClass={pnlCls(a.best_ps)} />
        <Tile label="Worst" value={signed(a.worst_ps)} valueClass={pnlCls(a.worst_ps)} />
      </div>

      <div className="text-[10px] uppercase tracking-wider text-dim mt-4.5 mb-2">
        Trades — live + simulated <span className="normal-case tracking-normal">{genAt}</span>
      </div>
      <div className="mt-1 max-h-[300px] overflow-y-auto bg-bg2 border border-line rounded-lg">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              {['Entry time', 'Symbol', 'Side', 'Entry', 'Exit', 'Reason', 'P&L/sh'].map((h) => (
                <th key={h} className="sticky top-0 bg-bg2 z-10 text-[10px] uppercase tracking-wide text-faint text-left px-2.5 py-1 border-b border-line font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {trades.map((t, i) => {
              const p = numOr(t.pnl_per_share)
              const tag = typeTag(t)
              return (
                <tr key={i} className={`cursor-pointer hover:bg-bg3 ${t.id === selId ? 'bg-[rgba(76,141,255,.12)] shadow-[inset_2px_0_0_var(--accent)]' : ''}`} onClick={() => t.id && setSelId(t.id)}>
                  <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">{pnlWhen(t.entry_time)}</td>
                  <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                    <b>{t.symbol || '?'}</b> <Chip tone={tag.tone} title={tag.title} className="!text-[9px]">{tag.label}</Chip>
                  </td>
                  <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                    <Badge direction={t.direction} />
                  </td>
                  <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">{fmt(t.entry, 2)}</td>
                  <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">{numOr(t.exit_price) === null ? '–' : fmt(t.exit_price, 2)}</td>
                  <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                    <Chip tone={reasonTone(t)}>{t.state === 'OPEN' || t.state === 'PARTIAL' ? (t.state === 'PARTIAL' ? 'partial' : 'open') : t.exit_reason || '?'}</Chip>
                  </td>
                  <td className={`px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px] ${pnlCls(p)}`}>{signed(p)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        {!trades.length ? (
          <div className="text-faint px-3.5 py-4.5 text-xs">No trades yet — accepted signals become paper positions; sims land here after “Simulate last day”.</div>
        ) : null}
      </div>

      {selId ? <PnlDetail id={selId} /> : null}
    </div>
  )
}

function PnlDetail({ id }: { id: string }) {
  const [d, setD] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const chartElRef = useRef<HTMLDivElement>(null)
  const chart = useChart(chartElRef)
  const [chartHint, setChartHint] = useState<string | null>(null)

  useEffect(() => {
    let stale = false
    setD(null)
    setErr(null)
    ;(async () => {
      try {
        const det = await jget<any>('/api/pnl/trade?id=' + encodeURIComponent(id))
        if (stale) return
        setD(det)
      } catch (e: any) {
        if (stale) return
        setErr(e.status === 404 ? 'Trade not found (state rotated?).' : 'Detail unavailable: ' + e.message)
      }
    })()
    return () => {
      stale = true
    }
  }, [id])

  useEffect(() => {
    if (!d) return
    let stale = false
    ;(async () => {
      chart.clear()
      try {
        const bars = await jget<any[]>('/api/candles?symbol=' + encodeURIComponent(d.symbol || ''))
        if (stale || !Array.isArray(bars) || !bars.length) throw new Error('no bars')
        setChartHint(null)
        chart.loadCandles(bars as any)
        const nearest = (iso: string) => {
          const target = parseBarTime(iso)
          let best = bars[0].time
          let bd = Math.abs(bars[0].time - target)
          for (const b of bars) {
            const dd = Math.abs(b.time - target)
            if (dd < bd) {
              bd = dd
              best = b.time
            }
          }
          return best
        }
        const buy = d.direction !== 'SELL'
        const lc = d.lifecycle || {}
        const markers: any[] = []
        if (d.entry_time)
          markers.push({
            time: nearest(d.entry_time),
            position: buy ? 'belowBar' : 'aboveBar',
            shape: buy ? 'arrowUp' : 'arrowDown',
            color: buy ? '#2fbf71' : '#e5534b',
            text: 'IN' + (numOr(d.entry) === null ? '' : ' ' + Number(d.entry).toFixed(2)),
          })
        for (const e of Array.isArray(lc.exits) ? lc.exits : []) {
          if (!e?.time) continue
          markers.push({
            time: nearest(e.time),
            position: buy ? 'aboveBar' : 'belowBar',
            shape: 'circle',
            color: '#d9a53f',
            text: String(e.reason || 'exit').toUpperCase() + (numOr(e.price) === null ? '' : ' ' + Number(e.price).toFixed(2)),
          })
        }
        chart.setMarkers(markers)
        chart.setPriceLines([
          { price: d.entry, color: '#4c8dff', title: 'ENTRY', style: 0, width: 1 },
          { price: lc.stop, color: '#e5534b', title: 'SL', style: 0, width: 2 },
          { price: lc.target1, color: '#2fbf71', title: 'T1' },
          { price: lc.target2, color: '#2fbf71', title: 'T2' },
        ])
        chart.fitContent()
      } catch {
        chart.clear()
        setChartHint('Candles no longer available for ' + (d.symbol || '?') + ' — old trade, chart skipped.')
      }
    })()
    return () => {
      stale = true
    }
    // `chart` is a fresh handle object every render (backed by stable refs) —
    // re-running this effect on it would just reload the same candles.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d])

  if (err) return <div className="mt-3 text-faint px-3.5 py-4.5 text-xs">{err}</div>
  if (!d) return <div className="mt-3 text-faint px-3.5 py-4.5 text-xs">Loading trade detail…</div>

  const lc = d.lifecycle || {}
  const hist = Array.isArray(lc.stop_history) ? lc.stop_history : []
  const auditA = d.entry_audit

  return (
    <div>
      <div className="bg-bg2 border border-line rounded-[10px] px-3.5 py-3 mt-3">
        <div className="text-[10px] uppercase tracking-wider text-dim mb-2">
          Narrative &amp; lifecycle — {d.symbol || '?'} {d.direction || ''}
          {d.source === 'sim' ? ' (SIM)' : ''} · {d.id || ''}
        </div>
        {Array.isArray(d.narrative) && d.narrative.length ? (
          d.narrative.map((s: string, i: number) => (
            <div key={i} className={`relative pl-4.5 py-0.5 text-[12.5px] before:content-[''] before:absolute before:left-1 before:top-2.5 before:w-1.5 before:h-1.5 before:rounded-full before:bg-faint ${tlClass(s)}`}>
              {s}
            </div>
          ))
        ) : (
          <div className="text-faint px-0.5 py-1">No narrative available.</div>
        )}
        <div className="text-[10px] uppercase tracking-wider text-dim mt-3 mb-2">Stop history</div>
        {hist.length ? (
          hist.map((h: any, i: number) => (
            <div key={i} className="font-mono text-[11px] text-dim pl-4.5 py-0.5 whitespace-nowrap overflow-hidden text-ellipsis">
              {hhmmss(h.time)}  {numOr(h.from) === null ? '     —' : fmt(h.from, 2).padStart(9)} -&gt; {fmt(h.to, 2)}   {h.reason || ''}
            </div>
          ))
        ) : (
          <div className="text-faint px-0.5 py-1">No stop history recorded (trade pre-dates stop-history logging).</div>
        )}
      </div>

      <div className="bg-bg2 border border-line rounded-[10px] px-3.5 py-3 mt-3">
        <div className="text-[10px] uppercase tracking-wider text-dim mb-2">Entry audit — agent tree at entry (read-only)</div>
        {auditA?.tree ? (
          <>
            <div className="text-[11px] text-faint font-mono mb-1.5">
              {auditA.symbol || '?'} {auditA.direction || '?'} · composite {fmt(auditA.score, 1)} / {fmt(auditA.threshold, 0)} ·{' '}
              {auditA.accepted
                ? 'ACCEPTED'
                : auditA.vetoed_by?.length
                  ? 'VETOED: ' + auditA.vetoed_by.map((v: any) => v.key + ' ' + fmt(v.score, 1) + ' < ' + fmt(v.floor, 0)).join(', ')
                  : 'rejected'}
              {auditA.evaluated_at ? ' · ' + hhmmss(auditA.evaluated_at) : ''}
            </div>
            <AgentTreeView tree={auditA.tree as TreeNode} readOnly />
          </>
        ) : (
          <div className="text-faint px-0.5 py-1">No stored entry audit found for this trade (paper log rotated or pre-dates audit logging).</div>
        )}
      </div>

      <div className="bg-bg2 border border-line rounded-[10px] px-3.5 py-3 mt-3">
        <div className="text-[10px] uppercase tracking-wider text-dim mb-2">Chart — {d.symbol || '?'} 5m · entry/exit markers + levels</div>
        <div className="h-[320px] relative">
          <div ref={chartElRef} className="absolute inset-0" />
          {chartHint ? <div className="absolute inset-0 flex items-center justify-center text-faint text-sm text-center pointer-events-none">{chartHint}</div> : null}
        </div>
      </div>
    </div>
  )
}

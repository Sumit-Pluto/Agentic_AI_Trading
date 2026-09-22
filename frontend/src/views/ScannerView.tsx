import type { SeriesMarker, Time } from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'
import { AgentTreeView } from '../components/AgentTree'
import { ChartContainer } from '../components/ChartPanel'
import { useChart } from '../hooks/useChart'
import { Badge, Chip, Empty, PanelTitle } from '../components/ui'
import { useVisiblePolling } from '../hooks/usePolling'
import { ApiError, jget, jpost } from '../lib/api'
import { fmt, hhmmss, numOr, parseBarTime } from '../lib/format'
import type { EvalResult, PositionsResponse, StateData, TreeNode } from '../lib/types'

function sigKey(s: EvalResult) {
  return s.symbol + '|' + s.direction + '|' + s.evaluated_at
}

function fmtOI(oi: number | null | undefined) {
  if (oi === null || oi === undefined) return ''
  if (oi >= 1e7) return (oi / 1e7).toFixed(1) + 'Cr'
  if (oi >= 1e5) return (oi / 1e5).toFixed(1) + 'L'
  if (oi >= 1e3) return (oi / 1e3).toFixed(0) + 'k'
  return String(Math.round(oi))
}

export interface ChartRequest {
  symbol: string
  direction: string
  bar_time?: string
  nonce: number
}

export function ScannerView({
  visible,
  stateData,
  chartRequest,
}: {
  visible: boolean
  stateData: StateData | null
  chartRequest?: ChartRequest | null
}) {
  const [current, setCurrent] = useState<EvalResult | null>(null)
  const [currentSig, setCurrentSig] = useState<string | null>(null)
  const [accOnly, setAccOnly] = useState(true)
  const [evalSym, setEvalSym] = useState('')
  const [evalMsg, setEvalMsg] = useState('')
  const [swingsOn, setSwingsOn] = useState(false)
  const [chartSub, setChartSub] = useState('5m candles')
  const [chartHint, setChartHint] = useState<string | null>('Select a signal or evaluate a symbol')

  const chartElRef = useRef<HTMLDivElement>(null)
  const chart = useChart(chartElRef)
  const chartSymbolRef = useRef<string | null>(null)
  const swingMarkersRef = useRef<SeriesMarker<Time>[]>([])
  const signalMarkerRef = useRef<SeriesMarker<Time> | null>(null)

  function applyMarkers() {
    const all = [...(signalMarkerRef.current ? [signalMarkerRef.current] : []), ...(swingsOn ? swingMarkersRef.current : [])]
    chart.setMarkers(all)
  }

  async function loadSwings(symbol: string) {
    try {
      const d = await jget<any>('/api/overlays?symbol=' + encodeURIComponent(symbol))
      if (symbol !== chartSymbolRef.current) return
      chart.setSupertrend(d.supertrend || [])
      chart.setPriceLines([
        { price: d.walls?.put_wall, color: 'rgba(47,191,113,.7)', title: 'PUT WALL' },
        { price: d.walls?.call_wall, color: 'rgba(229,83,75,.7)', title: 'CALL WALL' },
        ...(d.oi_levels || []).map((lv: any) => ({
          price: lv.strike,
          color: lv.side === 'PE' ? 'rgba(47,191,113,.32)' : 'rgba(229,83,75,.32)',
          title: 'OI ' + fmtOI(lv.oi),
          style: 3 as const,
          width: 1,
        })),
        ...(d.position
          ? [
              { price: d.position.entry, color: '#4c8dff', title: 'ENTRY', style: 0 as const, width: 1 },
              { price: d.position.stop, color: '#e5534b', title: 'SL', style: 0 as const, width: 2 },
              { price: d.position.target1, color: '#2fbf71', title: 'T1' },
              { price: d.position.target2, color: '#2fbf71', title: 'T2' },
            ]
          : []),
      ])
      const piv = Array.isArray(d.pivots) ? d.pivots : []
      swingMarkersRef.current = piv
        .filter((p: any) => p && p.time)
        .map((p: any) => {
          const isH = p.kind === 'H'
          const lbl = p.label || p.kind || '?'
          const up = lbl === 'HH' || lbl === 'HL'
          const dn = lbl === 'LH' || lbl === 'LL'
          return {
            time: p.time,
            position: isH ? 'aboveBar' : 'belowBar',
            shape: isH ? 'arrowDown' : 'arrowUp',
            color: up ? '#2fbf71' : dn ? '#e5534b' : '#8b949e',
            text: lbl,
          } as SeriesMarker<Time>
        })
      if (d.trend) setChartSub('5m candles · swings: ' + d.trend)
      applyMarkers()
    } catch {
      swingMarkersRef.current = []
    }
  }

  async function loadChart(symbol: string, direction: string, trigger: EvalResult['trigger']) {
    setChartSub('5m candles')
    chartSymbolRef.current = symbol
    chart.clear()
    try {
      const bars = await jget<any[]>('/api/candles?symbol=' + encodeURIComponent(symbol))
      chart.loadCandles(bars as any)
      if (trigger?.bar_time && bars.length) {
        const target = parseBarTime(trigger.bar_time)
        let best = bars[0].time
        let bd = Math.abs(bars[0].time - target)
        for (const b of bars) {
          const d = Math.abs(b.time - target)
          if (d < bd) {
            bd = d
            best = b.time
          }
        }
        signalMarkerRef.current = {
          time: best,
          position: direction === 'BUY' ? 'belowBar' : 'aboveBar',
          color: direction === 'BUY' ? '#2fbf71' : '#e5534b',
          shape: direction === 'BUY' ? 'arrowUp' : 'arrowDown',
          text: direction,
        } as SeriesMarker<Time>
      } else if (bars.length) {
        signalMarkerRef.current = {
          time: bars[bars.length - 1].time,
          position: 'aboveBar',
          color: '#8b949e',
          shape: 'circle',
          text: 'eval (' + direction + ')',
        } as SeriesMarker<Time>
      } else {
        signalMarkerRef.current = null
      }
      swingMarkersRef.current = []
      setChartHint(null)
      await loadSwings(symbol)
      applyMarkers()
      chart.fitContent()
    } catch {
      chart.clear()
      signalMarkerRef.current = null
      swingMarkersRef.current = []
      applyMarkers()
      setChartHint('No candle data for ' + symbol)
    }
  }

  function adoptResult(result: EvalResult, feedIdentity: string | null) {
    setCurrent(result)
    setCurrentSig(feedIdentity)
    loadChart(result.symbol, result.direction, result.trigger || null)
  }

  async function manualEvaluate(direction: 'BUY' | 'SELL') {
    const symbol = evalSym.trim().toUpperCase()
    if (!symbol) {
      setEvalMsg('enter a symbol first')
      return
    }
    setEvalMsg('evaluating ' + symbol + ' ' + direction + '…')
    try {
      const result = await jget<EvalResult>('/api/evaluate?symbol=' + encodeURIComponent(symbol) + '&direction=' + direction)
      setEvalMsg('')
      adoptResult(result, null)
    } catch (e) {
      const err = e as ApiError
      setEvalMsg(err.status === 404 ? 'no data for ' + symbol : 'error: ' + err.message)
    }
  }

  async function toggleAgentEnabled(node: TreeNode, enabled: boolean) {
    if (!current) return
    try {
      await jpost('/api/agents/toggle', { key: node.key, enabled })
      const result = await jget<EvalResult>('/api/evaluate?symbol=' + encodeURIComponent(current.symbol) + '&direction=' + current.direction)
      result.trigger = current.trigger || null
      setCurrent(result)
      setCurrentSig(null)
    } catch (e) {
      setEvalMsg('toggle failed: ' + (e as Error).message)
    }
  }

  async function onSwingsToggle(v: boolean) {
    setSwingsOn(v)
    if (v && chartSymbolRef.current) await loadSwings(chartSymbolRef.current)
    if (!v) setChartSub('5m candles')
  }
  useEffect(() => {
    applyMarkers()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [swingsOn])

  useEffect(() => {
    if (!chartRequest) return
    loadChart(chartRequest.symbol, chartRequest.direction, chartRequest.bar_time ? { bar_time: chartRequest.bar_time } : null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartRequest?.nonce])

  const signals = (stateData?.signals || []).filter((s) => (accOnly ? s.accepted : true))
  const treeSubtitle = current
    ? current.symbol +
      ' ' +
      current.direction +
      ' · composite ' +
      fmt(current.score, 1) +
      ' / ' +
      fmt(current.threshold, 0) +
      ' · ' +
      (current.accepted
        ? 'ACCEPTED'
        : current.vetoed_by?.length
          ? 'VETOED: ' + current.vetoed_by.map((v) => v.key + ' ' + fmt(v.score, 1) + ' < ' + fmt(v.floor, 0)).join(', ')
          : 'rejected') +
      (current.trigger?.armed_by
        ? ' · OBS "' + current.trigger.armed_by.label + '" ' + hhmmss(current.trigger.armed_by.bar_time) + ' + trend aligned'
        : '') +
      ' · ' +
      hhmmss(current.evaluated_at)
    : ''

  return (
    <div className={`flex flex-1 min-h-0 ${visible ? '' : 'hidden'}`}>
      <div className="w-[380px] shrink-0 flex flex-col border-r border-line bg-bg2 min-h-0">
        <div className="flex justify-between items-center px-3.5 pt-2.5 pb-1.5">
          <span className="text-[10px] uppercase tracking-wider text-dim">Signal feed</span>
          <label className="text-[10px] text-dim cursor-pointer flex items-center gap-1">
            <input type="checkbox" checked={accOnly} onChange={(e) => setAccOnly(e.target.checked)} className="accent-green" /> accepted only
          </label>
        </div>
        <div className="flex-1 overflow-y-auto min-h-0">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                {['Time', 'Symbol', 'Side', 'Price', 'Score'].map((h) => (
                  <th key={h} className="sticky top-0 bg-bg2 z-10 text-[10px] uppercase tracking-wide text-faint text-left px-2 py-1 border-b border-line font-medium">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {signals.map((s) => {
                const barT = s.trigger?.bar_time || s.evaluated_at
                const sel = sigKey(s) === currentSig
                const chipTone = s.score === null ? 'na' : s.accepted ? 'ok' : 'bad'
                return (
                  <tr
                    key={sigKey(s)}
                    className={`cursor-pointer hover:bg-bg3 ${sel ? 'bg-[rgba(76,141,255,.12)] shadow-[inset_2px_0_0_var(--accent)]' : ''}`}
                    onClick={() => adoptResult(s, sigKey(s))}
                  >
                    <td className="num px-2 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap">{hhmmss(barT)}</td>
                    <td className="px-2 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap">
                      <b>{s.symbol}</b>
                    </td>
                    <td className="px-2 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap">
                      <Badge direction={s.direction} />
                      {s.sim ? (
                        <Chip tone="na" className="ml-1 !text-[9px]">
                          SIM
                        </Chip>
                      ) : null}
                    </td>
                    <td className="num px-2 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap">{fmt(s.price, 2)}</td>
                    <td className="px-2 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap">
                      <Chip tone={chipTone}>{fmt(s.score, 1)}</Chip>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {!signals.length ? <Empty>No signals yet — waiting for sweep…</Empty> : null}
        </div>
        <div className="shrink-0 border-t border-line px-3.5 pt-2.5 pb-3.5">
          <div className="text-[10px] uppercase tracking-wider text-dim pb-0.5">Manual evaluate</div>
          <div className="flex gap-2 mt-1.5">
            <input
              type="text"
              placeholder="SYMBOL e.g. RELIANCE"
              spellCheck={false}
              autoComplete="off"
              list="symlist"
              value={evalSym}
              onChange={(e) => setEvalSym(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && manualEvaluate('BUY')}
              className="flex-1 bg-bg text-fg border border-line rounded-md px-2 py-1.5 font-mono text-xs outline-none focus:border-accent"
            />
            <button
              onClick={() => manualEvaluate('BUY')}
              className="text-green border border-[rgba(47,191,113,.5)] rounded-md px-3 py-1.5 text-xs hover:bg-green-bg"
            >
              BUY
            </button>
            <button
              onClick={() => manualEvaluate('SELL')}
              className="text-red border border-[rgba(229,83,75,.5)] rounded-md px-3 py-1.5 text-xs hover:bg-red-bg"
            >
              SELL
            </button>
          </div>
          <div className="text-[11px] text-amber mt-1.5 min-h-[14px]">{evalMsg}</div>
        </div>
      </div>

      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <div className="flex items-baseline gap-3 px-4 pt-2.5 pb-1 shrink-0">
          <span className="text-[15px] font-semibold font-mono">{current?.symbol ?? '—'}</span>
          <span className="text-[11px] text-dim font-mono">{chartSub}</span>
          <label className="ml-auto text-[11px] text-dim cursor-pointer flex items-center gap-1">
            <input type="checkbox" checked={swingsOn} onChange={(e) => onSwingsToggle(e.target.checked)} className="accent-accent" /> Swings
          </label>
        </div>
        <ChartContainer innerRef={chartElRef} hint={chartHint} />
        <div className="h-[42%] min-h-[200px] shrink-0 border-t border-line bg-bg2 flex flex-col">
          <div className="flex items-baseline gap-3 px-3.5 pt-2.5 pb-1.5 shrink-0">
            <span className="text-[10px] uppercase tracking-wider text-dim">Agent tree</span>
            <span className="text-[11px] text-faint font-mono">{treeSubtitle}</span>
          </div>
          <div className="flex-1 overflow-y-auto px-2 pb-3 min-h-0">
            {current?.tree ? <AgentTreeView tree={current.tree} onToggleEnabled={toggleAgentEnabled} /> : <Empty>Nothing evaluated yet.</Empty>}
          </div>
        </div>
        <PositionsStrip visible={visible} />
      </div>
    </div>
  )
}

function PositionsStrip({ visible }: { visible: boolean }) {
  const [data, setData] = useState<PositionsResponse | null>(null)
  const everSeen = useRef(false)

  useVisiblePolling(async () => {
    try {
      const d = await jget<PositionsResponse>('/api/positions')
      if (!(d.positions || []).length && !everSeen.current) return
      everSeen.current = true
      setData(d)
    } catch {
      /* best-effort */
    }
  }, 20000, visible)

  if (!data) return null
  const live = (data.positions || []).filter((p) => p.state !== 'CLOSED')
  const pnl = numOr(data.realized_pnl_today_per_share_weighted)

  return (
    <div className="shrink-0 border-t border-line bg-bg2 flex flex-col max-h-[170px]">
      <div className="flex items-center gap-5 px-3.5 pt-2 pb-1 shrink-0">
        <PanelTitle className="!p-0">Positions (paper)</PanelTitle>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Open</span>
          <span className="font-mono text-[13px]">{(numOr(data.open) || 0) + (numOr(data.partial) || 0)}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Realized today ₹/sh</span>
          <span className={`font-mono text-[13px] ${pnl === null ? '' : pnl >= 0 ? 'text-green' : 'text-red'}`}>
            {pnl === null ? '–' : (pnl >= 0 ? '+' : '') + pnl.toFixed(2)}
          </span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Trades today</span>
          <span className="font-mono text-[13px]">{numOr(data.closed_today) ?? '–'}</span>
        </div>
      </div>
      <div className="overflow-y-auto min-h-0">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              {['Symbol', 'Side', 'Entry', 'Stop', 'T1', 'State', 'Unreal/sh'].map((h) => (
                <th key={h} className="sticky top-0 bg-bg2 z-10 text-[10px] uppercase tracking-wide text-faint text-left px-2.5 py-0.5 border-b border-line font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {live.length ? (
              live.map((p, i) => {
                const unr = numOr(p.unrealized_per_share)
                return (
                  <tr key={i}>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                      <b>{p.symbol}</b>
                    </td>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                      <Badge direction={p.direction} />
                    </td>
                    <td className="num px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">{fmt(p.entry_price, 2)}</td>
                    <td className="num px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">{fmt(p.stop, 2)}</td>
                    <td className="num px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">{fmt(p.target1, 2)}</td>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                      <Chip tone={p.state === 'PARTIAL' ? 'ok' : 'na'}>{p.state ?? '?'}</Chip>
                    </td>
                    <td className={`num px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px] ${unr === null ? '' : unr >= 0 ? 'text-green' : 'text-red'}`}>
                      {unr === null ? 'n/a' : (unr >= 0 ? '+' : '') + unr.toFixed(2)}
                    </td>
                  </tr>
                )
              })
            ) : (
              <tr>
                <td colSpan={7} className="px-2.5 py-1 text-faint">
                  no live positions
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

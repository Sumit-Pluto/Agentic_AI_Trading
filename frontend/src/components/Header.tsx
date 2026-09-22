import { useEffect, useRef, useState } from 'react'
import { useVisiblePolling } from '../hooks/usePolling'
import { jget, jpost } from '../lib/api'
import { fmt, hhmmss } from '../lib/format'
import type { ActivityItem, ModeState, StateData } from '../lib/types'
import { StatItem } from './ui'

export type TabName = 'scanner' | 'swing' | 'agents' | 'manual' | 'advisor' | 'news' | 'pnl'

const TABS: { id: TabName; label: string; title?: string }[] = [
  { id: 'scanner', label: 'Scanner' },
  { id: 'swing', label: 'Swing' },
  { id: 'agents', label: 'Agents', title: 'Agent-primary signals — the agent tree fires buy/sell on its own (score + edge), no indicator trigger' },
  { id: 'manual', label: 'Manual' },
  { id: 'advisor', label: 'Strategy Advisor' },
  { id: 'news', label: 'News' },
  { id: 'pnl', label: 'P&L' },
]

export function Header({
  tab,
  onTab,
  stateData,
  onThresholdSet,
  onOpenLive,
  simBusy,
  simStatus,
  onSimulate,
  mode,
  onModeChange,
}: {
  tab: TabName
  onTab: (t: TabName) => void
  stateData: StateData | null
  onThresholdSet: (v: number) => void
  onOpenLive: () => void
  simBusy: boolean
  simStatus: string
  onSimulate: () => void
  mode: ModeState
  onModeChange: (m: ModeState) => void
}) {
  const thrRef = useRef<HTMLInputElement>(null)
  const thrFocused = useRef(false)

  useEffect(() => {
    if (thrRef.current && !thrFocused.current && stateData) {
      thrRef.current.value = String(stateData.signal_threshold)
    }
  }, [stateData])

  return (
    <header className="flex items-center gap-3.5 flex-wrap flex-shrink-0 px-4 py-2.5 bg-bg2 border-b border-line">
      <h1 className="text-[15px] font-semibold tracking-wide">
        NSE F&amp;O <span className="text-accent">Quant Scanner</span>
      </h1>
      <nav className="flex gap-1 flex-wrap ml-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            title={t.title}
            onClick={() => onTab(t.id)}
            className={`px-3 py-1.5 rounded-md text-xs border ${
              tab === t.id ? 'bg-bg3 text-accent border-line' : 'text-dim border-transparent hover:text-fg hover:border-line'
            }`}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <StatItem label="Universe" value={stateData?.universe ?? '–'} />
      <StatItem
        label="Last sweep"
        value={stateData?.last_sweep ? hhmmss(stateData.last_sweep) + ' (' + fmt(stateData.sweep_seconds, 1) + 's)' : 'never'}
      />
      <StatItem label="Paper trades" value={stateData?.paper_count ?? '–'} />

      <ModeButton mode={mode} onModeChange={onModeChange} onOpenLive={onOpenLive} />
      <PauseButton />
      <ActivityTicker />

      <div className="flex items-center gap-1.5 ml-auto">
        <button
          disabled={simBusy}
          onClick={onSimulate}
          title="Replay the last session bar-by-bar through the full pipeline (OBS + Supertrend + all agents). Great for weekends."
          className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs disabled:opacity-50 disabled:cursor-wait hover:border-accent hover:text-accent"
        >
          Simulate last day
        </button>
        <span className="text-[11px] text-amber min-w-[120px]">{simStatus}</span>
        <label htmlFor="thr-input" className="text-[10px] uppercase tracking-wide text-dim">
          Composite&nbsp;threshold
        </label>
        <input
          id="thr-input"
          ref={thrRef}
          type="number"
          step={1}
          min={0}
          max={100}
          onFocus={() => (thrFocused.current = true)}
          onBlur={() => (thrFocused.current = false)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') onThresholdSet(parseFloat((e.target as HTMLInputElement).value))
          }}
          className="bg-bg text-fg border border-line rounded-md px-2 py-1.5 font-mono text-xs outline-none focus:border-accent w-[72px]"
        />
        <button
          onClick={() => thrRef.current && onThresholdSet(parseFloat(thrRef.current.value))}
          className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent"
        >
          Set
        </button>
      </div>
    </header>
  )
}

function ModeButton({ mode, onModeChange, onOpenLive }: { mode: ModeState; onModeChange: (m: ModeState) => void; onOpenLive: () => void }) {
  async function onClick() {
    if (!mode.live) {
      onOpenLive()
      return
    }
    const openLive = mode.executor?.open_live || 0
    const warn = openLive > 0
      ? `\n\n⚠️ ${openLive} LIVE position(s) are OPEN. In paper mode their exit orders will NOT reach the broker — square them off in the broker terminal or stay LIVE until they close!`
      : ''
    if (!confirm('Switch back to PAPER mode?' + warn)) return
    try {
      const j = await jpost<ModeState>('/api/mode', { mode: 'paper' })
      onModeChange(j)
    } catch (e) {
      alert('Mode change failed: ' + (e as Error).message)
    }
  }

  const live = !!mode.live
  const day = mode.executor?.day
  return (
    <button
      onClick={onClick}
      title={
        live
          ? 'LIVE trading — real MIS orders on Shoonya!' +
            (day ? ` Today: ₹${(day.realized || 0).toFixed(0)} realized, ${day.orders || 0} orders.` : '') +
            ' Click to switch back to paper.'
          : 'PAPER mode (safe) — signals are logged, no orders. Click to enable LIVE real-money trading.'
      }
      className={`whitespace-nowrap border rounded-md px-3 py-1.5 text-xs font-semibold tracking-wide ${
        live ? 'text-white bg-red border-red live-pulse' : 'bg-bg3 border-line text-fg hover:border-accent hover:text-accent'
      }`}
    >
      {live ? '🔴 LIVE' : '📄 PAPER'}
    </button>
  )
}

function PauseButton() {
  const [paused, setPaused] = useState(false)
  const [busy, setBusy] = useState(false)

  useVisiblePolling(async () => {
    try {
      setPaused(!!(await jget<{ paused: boolean }>('/api/pause')).paused)
    } catch {
      /* keep last */
    }
  }, 30000)

  async function onClick() {
    setBusy(true)
    try {
      setPaused(!!(await jpost<{ paused: boolean }>('/api/pause', { paused: !paused })).paused)
    } catch {
      /* server briefly away */
    }
    setBusy(false)
  }

  return (
    <button
      disabled={busy}
      onClick={onClick}
      title={
        paused
          ? 'Scanner PAUSED — no new entries (exits keep managing). Click to resume.'
          : 'Kill switch: blocks NEW entries only — open positions keep managing.'
      }
      className={`whitespace-nowrap border rounded-md px-3 py-1.5 text-xs ${
        paused ? 'text-red bg-red-bg border-[rgba(229,83,75,.5)]' : 'bg-bg3 border-line text-fg hover:border-accent hover:text-accent'
      }`}
    >
      {paused ? '▶ Resume' : '⏸ Pause'}
    </button>
  )
}

function ActivityTicker() {
  const [items, setItems] = useState<ActivityItem[]>([])
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  useVisiblePolling(async () => {
    try {
      const d = await jget<ActivityItem[]>('/api/activity?n=30')
      setItems(Array.isArray(d) ? d : [])
    } catch {
      /* best-effort */
    }
  }, 15000)

  useEffect(() => {
    const onDocClick = (ev: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(ev.target as Node)) setOpen(false)
    }
    document.addEventListener('click', onDocClick)
    return () => document.removeEventListener('click', onDocClick)
  }, [])

  const first = items[0]
  return (
    <div ref={wrapRef} className="relative flex-1 min-w-[60px]">
      <div
        onClick={() => setOpen((v) => !v)}
        title={first ? `[${first.kind ?? '?'}] ${first.text ?? ''}` : 'click for recent activity'}
        className="text-[11px] text-dim font-mono whitespace-nowrap overflow-hidden text-ellipsis cursor-pointer select-none hover:text-fg"
      >
        {first ? `${hhmmss(first.time)}  ${first.text ?? ''}` : 'no activity yet'}
      </div>
      {open ? (
        <div className="absolute top-[calc(100%+10px)] right-0 w-[440px] max-w-[80vw] max-h-[320px] overflow-y-auto bg-bg2 border border-line rounded-lg px-2.5 py-1.5 z-[60] shadow-[0_8px_24px_rgba(0,0,0,.25)]">
          {items.length ? (
            items.slice(0, 30).map((a, i) => (
              <div key={i} className="px-0.5 py-1 text-[11.5px] border-b border-[rgba(38,48,63,.5)] last:border-b-0">
                <span className="num text-faint">{hhmmss(a.time)}</span>
                <span className="inline-block text-[9.5px] text-dim uppercase bg-[rgba(124,135,151,.12)] rounded-sm px-1 mx-1">{a.kind ?? '?'}</span>
                {a.text ?? ''}
              </div>
            ))
          ) : (
            <div className="text-faint px-0.5 py-1.5">no activity yet</div>
          )}
        </div>
      ) : null}
    </div>
  )
}

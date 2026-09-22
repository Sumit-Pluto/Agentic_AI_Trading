import { Fragment, useEffect, useState } from 'react'
import { jget, jpost } from '../lib/api'
import type { ModeState } from '../lib/types'

interface Check {
  ok: boolean
  name: string
  detail: string
}

const LIM_KEYS = [
  { key: 'risk_rupees', label: 'Risk per trade (₹)', sub: 'sizes each order: qty = risk ÷ stop-distance', def: 500 },
  { key: 'max_qty', label: 'Max shares per order', sub: 'hard quantity cap', def: 50 },
  { key: 'max_capital', label: 'Max capital per trade (₹)', sub: 'notional cap: qty × price', def: 50000 },
  { key: 'max_positions', label: 'Max open positions', sub: 'concurrent live trades', def: 3 },
  { key: 'max_daily_loss', label: 'Max daily loss (₹)', sub: 'realized loss that pauses new entries', def: 2000 },
] as const

export function LiveTradingModal({ open, onClose, mode, onModeChange }: { open: boolean; onClose: () => void; mode: ModeState; onModeChange: (m: ModeState) => void }) {
  const [checks, setChecks] = useState<Check[] | null>(null)
  const [checksErr, setChecksErr] = useState<string | null>(null)
  const [checksOk, setChecksOk] = useState(true)
  const [limits, setLimits] = useState<Record<string, number>>({})
  const [confirmText, setConfirmText] = useState('')
  const [testBusy, setTestBusy] = useState(false)
  const [testOut, setTestOut] = useState<{ ok: boolean; text: string }[] | null>(null)
  const [testProved, setTestProved] = useState<boolean | null>(null)
  const [goBusy, setGoBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    const lim = mode.executor?.limits || {}
    const init: Record<string, number> = {}
    for (const l of LIM_KEYS) init[l.key] = lim[l.key] ?? l.def
    setLimits(init)
    setConfirmText('')
    setTestOut(null)
    setTestProved(null)
    setChecks(null)
    setChecksErr(null)
    ;(async () => {
      try {
        const j = await jget<{ ok: boolean; checks: Check[] }>('/api/broker/check')
        setChecks(j.checks || [])
        setChecksOk(!!j.ok)
      } catch (e) {
        setChecksErr((e as Error).message)
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  if (!open) return null

  async function runOrderTest() {
    if (!confirm('Send ONE real 1-share limit order priced ~20% below market (cannot fill) and cancel it immediately?')) return
    setTestBusy(true)
    setTestOut([{ ok: true, text: 'testing order path…' }])
    try {
      const j = await jpost<{ steps: Check[]; proved: boolean }>('/api/broker/ordertest', { confirm: 'TEST' })
      setTestOut((j.steps || []).map((s) => ({ ok: s.ok, text: `${s.name}: ${s.detail}` })))
      setTestProved(!!j.proved)
    } catch (e) {
      setTestOut([{ ok: false, text: 'order test failed: ' + (e as Error).message }])
      setTestProved(null)
    }
    setTestBusy(false)
  }

  async function goLive() {
    setGoBusy(true)
    try {
      const j = await jpost<ModeState>('/api/mode', { mode: 'live', confirm: 'LIVE', limits })
      onModeChange(j)
      if ((j as any).live_open > 0 && !j.live) {
        alert(`⚠️ ${(j as any).live_open} live position(s) still open at the broker — manage them manually in the Shoonya terminal!`)
      }
      onClose()
    } catch (e) {
      alert('Going live failed: ' + (e as Error).message)
    }
    setGoBusy(false)
  }

  return (
    <div className="fixed inset-0 z-[100] bg-black/60 flex items-center justify-center" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="w-[460px] max-w-[92vw] max-h-[88vh] overflow-y-auto bg-bg2 border border-red rounded-lg p-4 shadow-2xl">
        <h2 className="text-sm text-red mb-2">⚠️ Enable LIVE real-money trading</h2>
        <div className="text-[11.5px] text-dim my-1.5 mb-2.5 leading-relaxed">
          Real intraday (MIS) orders will be placed on your Shoonya account: entries, stop-losses, targets and the 15:12 square-off. New
          entries are only taken between <b>09:15–15:15 IST</b>; per-trade quantity = risk ÷ (entry − stop), under the caps below.
        </div>
        <div className="font-mono text-[11.5px] my-1.5 mb-2.5 leading-relaxed">
          {checksErr ? (
            <div className="text-red">✗ broker check failed: {checksErr}</div>
          ) : checks === null ? (
            'running broker checks…'
          ) : (
            <>
              {checks.map((c, i) => (
                <div key={i} className={c.ok ? 'text-green' : 'text-red'}>
                  {c.ok ? '✓' : '✗'} {c.name}: {c.detail}
                </div>
              ))}
              {!checksOk ? <div className="text-red">⚠ fix the failures above before going live</div> : null}
            </>
          )}
        </div>
        <div className="grid grid-cols-[1fr_110px] gap-x-2.5 gap-y-1.5 items-center my-2">
          {LIM_KEYS.map((l) => (
            <Fragment key={l.key}>
              <label className="text-xs text-fg">
                {l.label}
                <span className="block text-[10px] text-faint">{l.sub}</span>
              </label>
              <input
                type="number"
                value={limits[l.key] ?? ''}
                onChange={(e) => setLimits((p) => ({ ...p, [l.key]: Number(e.target.value) }))}
                className="bg-bg text-fg border border-line rounded-md px-2 py-1.5 text-xs w-full font-mono text-right"
              />
            </Fragment>
          ))}
        </div>
        <button
          disabled={testBusy}
          onClick={runOrderTest}
          title="Sends ONE real 1-share limit order priced 20% below market (cannot fill) and cancels it immediately — proves the buy/sell pipeline."
          className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent disabled:opacity-50"
        >
          Test order path (place + cancel 1 unfillable share)
        </button>
        <div className="font-mono text-[11px] mt-1.5 leading-relaxed">
          {testOut?.map((s, i) => (
            <div key={i} className={s.ok ? 'text-green' : 'text-red'}>
              {s.ok ? '✓' : '✗'} {s.text}
            </div>
          ))}
          {testProved !== null ? (
            <div className={testProved ? 'text-green' : 'text-red'}>
              <b>{testProved ? '✓ order pipeline proven — Shoonya accepts our buy/sell requests' : '✗ order pipeline NOT proven'}</b>
            </div>
          ) : null}
        </div>
        <div className="flex gap-2 items-center mt-3">
          <input
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            placeholder="type LIVE to confirm"
            autoComplete="off"
            className="flex-1 bg-bg text-red border border-red rounded-md px-2.5 py-1.5 text-sm font-bold tracking-widest font-mono"
          />
          <button
            disabled={confirmText !== 'LIVE' || goBusy}
            onClick={goLive}
            className="bg-red text-white border border-red rounded-md px-3 py-1.5 text-xs font-bold disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Go LIVE
          </button>
          <button onClick={onClose} className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent">
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}

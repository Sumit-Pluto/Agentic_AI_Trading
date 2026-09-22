import { useEffect, useState } from 'react'
import { jget, jpost, wlAdd } from '../lib/api'
import { fmt } from '../lib/format'

interface SwingSignal {
  symbol: string
  segment: string
  interval: string
  direction: string
  price: number
  entry: number
  stop: number
  target: number
  rr: number
  ob_bottom: number
  ob_top: number
}

interface ParamSpec {
  type?: 'bool' | 'number'
  min?: number
  max?: number
}

const SWING_PARAM_LABELS: Record<string, string> = {
  lookback: 'Divergence lookback',
  min_bars: 'Min bars between pivots',
  mom_len: 'Momentum length',
  rsi_len: 'RSI length',
  rsi_upper: 'RSI upper band',
  rsi_lower: 'RSI lower band',
  use_stoch: 'Stoch-RSI confirm',
  stoch_win: 'Stoch confirm window',
  st_rsi_len: 'StochRSI · RSI len',
  st_len: 'StochRSI · stoch len',
  st_ksm: 'StochRSI · %K smooth',
  st_dsm: 'StochRSI · %D smooth',
}

export function SwingView({ visible }: { visible: boolean }) {
  const [rows, setRows] = useState<SwingSignal[]>([])
  const [meta, setMeta] = useState('')
  const [paramsOpen, setParamsOpen] = useState(false)

  async function refresh() {
    try {
      const j = await jget<{ count: number; last_scan: string; signals: SwingSignal[] }>('/api/swing')
      setMeta(`${j.count || 0} signals · ${j.last_scan || 'not scanned yet'}`)
      setRows(j.signals || [])
    } catch (e) {
      setMeta('error: ' + (e as Error).message)
    }
  }

  useEffect(() => {
    if (visible) refresh()
  }, [visible])

  return (
    <div className={`flex-1 min-h-0 overflow-y-auto px-5 py-4 ${visible ? '' : 'hidden'}`}>
      <div className="font-bold mb-2.5 flex items-center gap-2.5">
        Swing — Rbknox + Order Block <span className="text-dim font-normal">(Cash / F&amp;O / MCX · daily + intraday)</span>
        <span className="text-dim font-normal">{meta}</span>
        <button
          onClick={() => setParamsOpen((v) => !v)}
          title="Edit the Rbknox (Knoxville Divergence) parameters"
          className="text-xs px-2 py-0.5 bg-bg3 border border-line rounded hover:border-accent hover:text-accent"
        >
          ⚙ Rbknox params
        </button>
        <button onClick={refresh} className="text-xs px-2 py-0.5 bg-bg3 border border-line rounded hover:border-accent hover:text-accent">
          ↻
        </button>
      </div>
      {paramsOpen ? <SwingParamsEditor onSaved={refresh} /> : null}
      <div className="overflow-x-auto">
        <table className="border-collapse w-full text-xs">
          <thead>
            <tr>
              {['Symbol', 'Seg', 'TF', 'Dir', 'Price', 'Entry', 'Stop', 'Target', 'RR', 'OB zone', 'Rbknox'].map((h) => (
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
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">
                    <b>{s.symbol}</b>
                  </td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{s.segment}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{s.interval}</td>
                  <td className={`px-2.5 py-1 border-b border-line whitespace-nowrap ${s.direction === 'BUY' ? 'text-green' : 'text-red'}`}>{s.direction}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{fmt(s.price)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{fmt(s.entry)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{fmt(s.stop)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{fmt(s.target)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{fmt(s.rr)}</td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">
                    {fmt(s.ob_bottom)}–{fmt(s.ob_top)}
                  </td>
                  <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">
                    <button
                      onClick={() => wlAdd({ symbol: s.symbol, strategy: 'swing', direction: s.direction, segment: s.segment })}
                      className="text-[11px] px-2 py-0.5 bg-bg3 border border-line rounded hover:border-accent hover:text-accent"
                    >
                      + watch
                    </button>
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={11} className="px-2.5 py-1 text-dim">
                  No swing signals right now.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SwingParamsEditor({ onSaved }: { onSaved: () => void }) {
  const [spec, setSpec] = useState<Record<string, ParamSpec> | null>(null)
  const [defaults, setDefaults] = useState<Record<string, number | boolean>>({})
  const [values, setValues] = useState<Record<string, number | boolean>>({})
  const [status, setStatus] = useState('')

  useEffect(() => {
    ;(async () => {
      try {
        const j = await jget<{ spec: Record<string, ParamSpec>; defaults: Record<string, number | boolean>; params: Record<string, number | boolean> }>(
          '/api/swing/params',
        )
        setSpec(j.spec || {})
        setDefaults(j.defaults || {})
        setValues(j.params || {})
      } catch (e) {
        setStatus('params unavailable: ' + (e as Error).message)
      }
    })()
  }, [])

  if (!spec) return <div className="my-2 px-3.5 py-3 border border-line rounded-lg bg-bg2 text-red text-xs">{status || 'loading…'}</div>

  async function save() {
    setStatus('saving…')
    try {
      const r = await jpost<{ params?: Record<string, number | boolean> }>('/api/swing/params', values)
      if (r.params) setValues(r.params)
      setStatus('saved · rescanning…')
      setTimeout(onSaved, 1500)
    } catch (e) {
      setStatus('error: ' + (e as Error).message)
    }
  }

  function reset() {
    setValues({ ...defaults })
    setStatus('defaults loaded — Save to apply')
  }

  return (
    <div className="my-2 mb-4 px-3.5 py-3 border border-line rounded-lg bg-bg2">
      <div className="text-dim mb-2 text-xs">Rbknox parameters — edits re-scan the whole universe.</div>
      <div className="grid gap-x-4.5 gap-y-2" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))' }}>
        {Object.entries(spec).map(([k, sp]) => (
          <label key={k} className="flex items-center justify-between gap-2 text-xs text-dim" title={sp.type !== 'bool' ? `${sp.min}–${sp.max}` : undefined}>
            <span>{SWING_PARAM_LABELS[k] || k}</span>
            {sp.type === 'bool' ? (
              <input
                type="checkbox"
                checked={!!values[k]}
                onChange={(e) => setValues((p) => ({ ...p, [k]: e.target.checked }))}
              />
            ) : (
              <input
                type="number"
                value={values[k] as number}
                min={sp.min}
                max={sp.max}
                step={1}
                onChange={(e) => setValues((p) => ({ ...p, [k]: Number(e.target.value) }))}
                className="bg-bg text-fg border border-line rounded-md px-2 py-1 text-xs w-[66px]"
              />
            )}
          </label>
        ))}
      </div>
      <div className="mt-3 flex items-center gap-2.5">
        <button onClick={save} className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent">
          Save &amp; rescan
        </button>
        <button onClick={reset} className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent">
          Reset defaults
        </button>
        <span className="text-[11px] text-dim">{status}</span>
      </div>
    </div>
  )
}

import { useState } from 'react'
import { fmt } from '../lib/format'

export function ManualView({ visible }: { visible: boolean }) {
  const [sym, setSym] = useState('')
  const [seg, setSeg] = useState('')
  const [qty, setQty] = useState(1)
  const [scoreHtml, setScoreHtml] = useState<{ text: string; err?: boolean } | null>(null)
  const [outMsg, setOutMsg] = useState<{ text: string; color: string } | null>(null)

  async function evaluate(direction: 'BUY' | 'SELL') {
    const s = sym.trim().toUpperCase()
    if (!s) return
    setScoreHtml({ text: 'scoring…' })
    try {
      const url = `/api/evaluate?symbol=${encodeURIComponent(s)}&direction=${direction}` + (seg ? `&segment=${seg}` : '')
      const j = await (await fetch(url)).json()
      if (j.error) {
        setScoreHtml({ text: j.error, err: true })
        return
      }
      const acc = j.accepted ? 'ACCEPTED' : 'rejected'
      setScoreHtml({
        text:
          `${s} ${direction} · score ${fmt(j.score, 0)}/${fmt(j.threshold, 0)} ${acc} · seg ${j.segment || ''} · px ${fmt(j.price)}` +
          (j.vetoed_by ? ` · veto: ${j.vetoed_by.map((v: { key: string }) => v.key).join(', ')}` : ''),
      })
    } catch (e) {
      setScoreHtml({ text: 'error: ' + (e as Error).message, err: true })
    }
  }

  async function order(direction: 'BUY' | 'SELL') {
    const s = sym.trim().toUpperCase()
    if (!s || !(qty > 0)) {
      setOutMsg({ text: 'enter a symbol and qty', color: '' })
      return
    }
    if (!confirm(`⚠️ REAL ORDER — ${direction} ${qty} ${s} (${seg || 'auto'}) on Shoonya now?`)) return
    setOutMsg({ text: direction + '…', color: 'var(--amber)' })
    try {
      const r = await fetch('/api/manual/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: s, direction, qty, segment: seg, confirm: direction }),
      })
      const j = await r.json()
      if (!r.ok) {
        setOutMsg({ text: '✗ ' + (j.error || r.status), color: 'var(--red)' })
        return
      }
      setOutMsg({
        text: `${j.direction} ${j.tsym} x${j.qty} ${j.status}` + (j.fill_price ? ` @${j.fill_price}` : '') + (j.reject_reason ? ` (${j.reject_reason})` : ''),
        color: j.status === 'COMPLETE' ? 'var(--green)' : 'var(--amber)',
      })
    } catch (e) {
      setOutMsg({ text: '✗ ' + (e as Error).message, color: 'var(--red)' })
    }
  }

  return (
    <div className={`flex-1 min-h-0 overflow-y-auto px-5 py-4 max-w-[760px] ${visible ? '' : 'hidden'}`}>
      <div className="font-bold mb-2.5">Manual — search a stock, get the agent score, then trade it</div>
      <div className="flex gap-2 items-center flex-wrap my-1.5">
        <input
          value={sym}
          onChange={(e) => setSym(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && evaluate('BUY')}
          placeholder="SYMBOL (e.g. RELIANCE, CRUDEOIL)"
          className="bg-bg text-fg border border-line rounded-md px-2 py-1.5 text-xs font-mono"
        />
        <select value={seg} onChange={(e) => setSeg(e.target.value)} className="bg-bg text-fg border border-line rounded-md px-2 py-1.5 text-xs">
          <option value="">auto</option>
          <option>CASH</option>
          <option>FNO</option>
          <option>MCX</option>
        </select>
        <button onClick={() => evaluate('BUY')} className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent">
          Score BUY
        </button>
        <button onClick={() => evaluate('SELL')} className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent">
          Score SELL
        </button>
      </div>
      <div className={`my-2 text-xs ${scoreHtml?.err ? 'text-red' : 'text-dim'}`}>{scoreHtml?.text}</div>
      <div className="flex gap-2 items-center flex-wrap my-1.5">
        <input
          type="number"
          min={1}
          value={qty}
          onChange={(e) => setQty(Number(e.target.value))}
          placeholder="qty"
          className="bg-bg text-fg border border-line rounded-md px-2 py-1.5 text-xs font-mono w-[120px]"
        />
        <button
          onClick={() => order('BUY')}
          className="text-green border border-[rgba(47,191,113,.5)] rounded-md px-3 py-1.5 text-xs hover:bg-green-bg"
        >
          🟢 BUY (real)
        </button>
        <button onClick={() => order('SELL')} className="text-red border border-[rgba(229,83,75,.5)] rounded-md px-3 py-1.5 text-xs hover:bg-red-bg">
          🔴 SELL (real)
        </button>
        <span className="text-dim text-xs" style={{ color: outMsg?.color || undefined }}>
          {outMsg?.text}
        </span>
      </div>
      <div className="text-dim text-[11px] mt-1.5">
        Places a REAL Shoonya order (you confirm first). CASH = shares · FNO/MCX = lots. Kill-switch respected.
      </div>
    </div>
  )
}

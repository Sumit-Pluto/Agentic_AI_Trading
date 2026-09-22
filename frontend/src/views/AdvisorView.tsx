import { useEffect, useRef, useState } from 'react'
import { Empty, Spin, Tile } from '../components/ui'
import { jget, jpost } from '../lib/api'
import { inr, numOr, pctIv } from '../lib/format'

/* ── shared helpers ─────────────────────────────────────────────────── */

function wallInfo(w: any): { strike: number; oi: number | null } | null {
  if (w === null || w === undefined) return null
  if (typeof w === 'number') return { strike: w, oi: null }
  const k = numOr(w.strike !== undefined ? w.strike : w.k)
  return k === null ? null : { strike: k, oi: numOr(w.oi) }
}

function confColor(c: unknown): string {
  const u = String(c || '').toUpperCase()
  if (u === 'HIGH') return 'text-green bg-green-bg'
  if (u === 'MED_HIGH' || u === 'MEDIUM_HIGH' || u === 'MEDHIGH') return 'text-[#e8833a] bg-[rgba(232,131,58,.16)]'
  if (u.startsWith('MED')) return 'text-amber bg-[rgba(217,165,63,.16)]'
  return 'text-dim bg-[rgba(124,135,151,.16)]'
}

function normAdvice(d: any) {
  const m = d.metrics || d.summary || d
  const v = d.view || d.market_view || {}
  const rec = d.recommendations || d.recommendation || d.recommend || d
  const dq = d.data_quality || {}
  const volMult = numOr(v.vol_mult !== undefined ? v.vol_mult : d.vol_mult)
  let volView = d.vol_view || v.vol_view || v.vol_tag || d.vol_regime || null
  if (!volView && volMult !== null) volView = volMult > 1 ? 'iv_cheap' : volMult < 1 ? 'iv_rich' : 'normal'
  return {
    symbol: d.symbol || m.symbol || '',
    spot: numOr(m.spot !== undefined ? m.spot : d.spot),
    lot: numOr(m.lot !== undefined ? m.lot : d.lot),
    expiryText: typeof d.expiry === 'string' && d.expiry ? d.expiry : null,
    expiryEpoch: numOr(m.expiry_epoch !== undefined ? m.expiry_epoch : d.expiry_epoch),
    dte: numOr(m.dte_days !== undefined ? m.dte_days : d.dte_days !== undefined ? d.dte_days : d.dte),
    atmIv: numOr(m.atm_iv),
    ivPctile: numOr(m.atm_iv_percentile !== undefined ? m.atm_iv_percentile : m.iv_percentile),
    pcr: numOr(m.pcr_oi),
    pcrFlag: m.pcr_flag || null,
    maxPain: numOr(m.max_pain),
    putWall: wallInfo(m.put_wall),
    callWall: wallInfo(m.call_wall),
    bias: numOr(v.bias !== undefined ? v.bias : d.bias),
    volView,
    rangeConv: numOr(v.range_conviction !== undefined ? v.range_conviction : d.range_conviction),
    provisional: !!(m.provisional || dq.provisional),
    chainThin: !!m.chain_thin,
    footprints: Array.isArray(d.footprints) ? d.footprints : [],
    generated: Array.isArray(rec.generated) ? rec.generated : [],
    benchmarks: Array.isArray(rec.benchmarks) ? rec.benchmarks : [],
    note: rec.note || d.note || null,
    disclaimer: rec.disclaimer || d.disclaimer || null,
    sizing: rec.sizing_note || d.sizing_note || null,
  }
}

function fmtStrikes(ks: unknown): string {
  if (!Array.isArray(ks) || !ks.length) return ''
  return ks.map((k) => (typeof k === 'number' ? String(k) : String(k))).join(' / ')
}

function BiasTile({ b }: { b: number | null }) {
  const w = b === null ? 0 : Math.min(1, Math.abs(b)) * 50
  const left = b !== null && b >= 0 ? 50 : 50 - w
  const col = b !== null && b >= 0 ? 'var(--green)' : 'var(--red)'
  const txt = b === null ? 'n/a' : (b >= 0 ? '+' : '') + b.toFixed(2)
  const tag = b === null ? '' : b >= 0.15 ? 'bullish' : b <= -0.15 ? 'bearish' : 'neutral'
  return (
    <div className="bg-bg2 border border-line rounded-lg px-3 py-2 min-w-[96px]">
      <span className="block text-[10px] uppercase tracking-wide text-dim">
        Bias {tag ? <>&middot; {tag}</> : null} <span className="num">{txt}</span>
      </span>
      <div className="relative w-[150px] h-2 mt-1.5 bg-bg3 border border-line rounded">
        <div className="absolute left-1/2 -top-[3px] -bottom-[3px] w-px bg-faint" />
        {b !== null ? <div className="absolute top-px bottom-px rounded-sm" style={{ left: left + '%', width: w + '%', background: col }} /> : null}
      </div>
    </div>
  )
}

function StratCard({ s, onSave, saved }: { s: any; onSave?: () => void; saved?: boolean }) {
  const rr = numOr(s.reward_risk)
  const good = rr !== null && rr >= 0.5
  const flow = s.net_flow || (numOr(s.net_premium_per_share) !== null ? (s.net_premium_per_share > 0 ? 'DEBIT' : 'CREDIT') : '')
  const nps = numOr(s.net_premium_per_share)
  const legs = Array.isArray(s.legs) ? s.legs : []
  const maxP = s.max_profit_per_lot === 'UNLIMITED' ? 'UNLIMITED' : inr(s.max_profit_per_lot)
  const bes = Array.isArray(s.breakevens) && s.breakevens.length ? s.breakevens.map((b: number) => Number(b).toFixed(1)).join(' / ') : 'n/a'
  const pop = numOr(s.pop_pct)
  const eP = numOr(s.expected_pnl_per_lot)
  const score = numOr(s.score !== undefined ? s.score : s.rank_score)
  const popClassic = numOr(s.pop_classic_pct)

  return (
    <div className={`bg-bg2 border rounded-[9px] p-3.5 flex flex-col gap-2 ${good ? 'border-[rgba(47,191,113,.55)]' : 'border-line'}`}>
      <h3 className="text-[14.5px] font-semibold flex items-center gap-2">
        {s.label || s.name || 'structure'}
        <span className="text-[11px] font-mono text-dim font-normal">
          {flow} {nps !== null ? Math.abs(nps).toFixed(2) + '/sh' : ''}
        </span>
        {onSave ? (
          <button
            disabled={saved}
            onClick={onSave}
            title="Save to Watchlist with live P&amp;L"
            className="ml-auto text-[10.5px] px-2 py-0.5 border border-line rounded bg-bg3 text-accent cursor-pointer hover:border-accent disabled:text-green disabled:border-green disabled:cursor-default"
          >
            {saved ? '✓ saved' : '💾 save'}
          </button>
        ) : null}
      </h3>
      {legs.length ? (
        <table className="w-full border-collapse">
          <thead>
            <tr>
              {['Side', 'Type', 'Strike', 'Premium'].map((h) => (
                <th key={h} className="text-[9.5px] uppercase tracking-wide text-faint text-left px-1.5 py-0.5 border-b border-line font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {legs.map((l: any, i: number) => {
              const buy = String(l.side).toUpperCase() === 'BUY'
              return (
                <tr key={i} title={l.tsym || ''}>
                  <td className={`px-1.5 py-0.5 font-mono text-[11.5px] border-b border-[rgba(38,48,63,.5)] ${buy ? 'text-green font-bold' : 'text-red font-bold'}`}>
                    {l.side}
                  </td>
                  <td className="px-1.5 py-0.5 font-mono text-[11.5px] border-b border-[rgba(38,48,63,.5)]">{l.type || (l.is_call ? 'CE' : 'PE')}</td>
                  <td className="px-1.5 py-0.5 font-mono text-[11.5px] border-b border-[rgba(38,48,63,.5)]">{String(l.strike)}</td>
                  <td className="px-1.5 py-0.5 font-mono text-[11.5px] border-b border-[rgba(38,48,63,.5)]">{numOr(l.premium) === null ? 'n/a' : Number(l.premium).toFixed(2)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : null}
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
        <span className="text-[10px] uppercase tracking-wide text-dim">Max profit / lot</span>
        <span className="font-mono text-xs text-green">{maxP}</span>
        <span className="text-[10px] uppercase tracking-wide text-dim">Max loss / lot</span>
        <span className="font-mono text-xs text-red">{inr(s.max_loss_per_lot)}</span>
        <span className="text-[10px] uppercase tracking-wide text-dim">Breakevens</span>
        <span className="font-mono text-xs">{bes}</span>
        <span className="text-[10px] uppercase tracking-wide text-dim" title="P(profit beyond round-trip friction) under fat-tailed (Student-t) returns at the legs own IVs — the honest number">
          POP (net)
        </span>
        <span className="font-mono text-xs">{pop === null ? 'n/a' : pop.toFixed(1) + '%'}</span>
        {popClassic !== null && popClassic !== pop ? (
          <>
            <span className="text-[10px] uppercase tracking-wide text-dim" title="the old frictionless lognormal-at-ATM-IV POP — kept to show the optimism gap">
              POP (classic)
            </span>
            <span className="font-mono text-xs text-dim">{popClassic.toFixed(1)}%</span>
          </>
        ) : null}
        <span className="text-[10px] uppercase tracking-wide text-dim">Reward : risk</span>
        <span className="font-mono text-xs">{rr === null ? 'n/a' : rr.toFixed(2)}</span>
        <span className="text-[10px] uppercase tracking-wide text-dim">E[P&amp;L] / lot</span>
        <span className={`font-mono text-xs ${eP !== null && eP < 0 ? 'text-red' : 'text-green'}`}>{inr(eP)}</span>
        <span className="text-[10px] uppercase tracking-wide text-dim">Score</span>
        <span className="font-mono text-xs">{score === null ? 'n/a' : score.toFixed(3)}</span>
        {s.margin_note ? (
          <>
            <span className="text-[10px] uppercase tracking-wide text-dim">Margin</span>
            <span className="font-mono text-[11px]">{s.margin_note}</span>
          </>
        ) : null}
      </div>
      {s.rationale ? <div className="text-[11.5px] text-dim border-t border-line pt-1.5">{s.rationale}</div> : null}
    </div>
  )
}

/* ── advisor sub-panel ──────────────────────────────────────────────── */

function AdvisorPanel({ onModelUpdate }: { onModelUpdate: (m: any) => void }) {
  const [sym, setSym] = useState('')
  const [profile, setProfile] = useState('balanced')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState<React.ReactNode>('')
  const [advice, setAdvice] = useState<ReturnType<typeof normAdvice> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [savedKeys, setSavedKeys] = useState<Set<string>>(new Set())

  async function analyze() {
    const s = sym.trim().toUpperCase()
    if (!s) {
      setStatus('enter a symbol first')
      return
    }
    if (busy) return
    setBusy(true)
    setError(null)
    setStatus(
      <>
        <Spin />
        analyzing {s} — chain + OI history crunch, may take a while…
      </>,
    )
    try {
      const d = await jget<any>(`/api/strategist?symbol=${encodeURIComponent(s)}&profile=${encodeURIComponent(profile)}`)
      setStatus('')
      if (d && d.error) {
        setError(s + ': ' + d.error)
        setAdvice(null)
      } else {
        const a = normAdvice(d)
        setAdvice(a)
        onModelUpdate(a)
        setSavedKeys(new Set())
      }
    } catch (e: any) {
      setStatus('')
      const msg = e.status === 503 ? `No broker session on the server (${e.message}) — log in and retry.` : e.status === 404 ? `No usable option chain for ${s}.` : `Error: ${e.message}`
      setError(msg)
      setAdvice(null)
    }
    setBusy(false)
  }

  async function save(list: 'generated' | 'benchmarks', i: number) {
    if (!advice) return
    const s = advice[list][i]
    const payload = {
      symbol: advice.symbol,
      label: s.label || s.name || 'structure',
      legs: (s.legs || []).map((l: any) => ({ side: l.side, type: l.type || (l.is_call ? 'CE' : 'PE'), strike: l.strike, premium: l.premium, tsym: l.tsym })),
      net_premium_per_share: s.net_premium_per_share,
      lot: advice.lot,
      metrics: { max_profit_per_lot: s.max_profit_per_lot, max_loss_per_lot: s.max_loss_per_lot, pop_pct: s.pop_pct, reward_risk: s.reward_risk, breakevens: s.breakevens },
    }
    try {
      await jpost('/api/strategist/save', payload)
      setSavedKeys((prev) => new Set(prev).add(list + i))
    } catch {
      /* leave unsaved, button stays active for retry */
    }
  }

  return (
    <div>
      <div className="flex items-center gap-2 mb-3.5">
        <input
          value={sym}
          onChange={(e) => setSym(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && analyze()}
          placeholder="SYMBOL e.g. RELIANCE"
          spellCheck={false}
          autoComplete="off"
          list="symlist"
          className="bg-bg text-fg border border-line rounded-md px-2 py-1.5 text-xs font-mono"
        />
        <select
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          title="Risk style: controls the minimum probability of profit and the ranking objective"
          className="bg-bg text-fg border border-line rounded-md px-2 py-1.5 text-xs"
        >
          <option value="conservative">Conservative — POP ≥ 60%</option>
          <option value="balanced">Balanced — POP ≥ 40%</option>
          <option value="aggressive">Aggressive — POP ≥ 25%, max R:R</option>
        </select>
        <button disabled={busy} onClick={analyze} className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent disabled:opacity-50">
          Analyze
        </button>
        <span className="text-xs text-amber">{status}</span>
      </div>
      {error ? <div className="text-red bg-red-bg border border-[rgba(229,83,75,.4)] rounded-lg px-3.5 py-3 text-sm max-w-[720px]">{error}</div> : null}
      {advice ? <AdvisorBody a={advice} onSave={save} savedKeys={savedKeys} /> : !error ? (
        <Empty>
          Enter a symbol and hit Analyze — the advisor reads the live option chain + OI history and proposes strategy structures (views, not orders).
        </Empty>
      ) : null}
    </div>
  )
}

function AdvisorBody({ a, onSave, savedKeys }: { a: ReturnType<typeof normAdvice>; onSave: (list: 'generated' | 'benchmarks', i: number) => void; savedKeys: Set<string> }) {
  let expTxt = 'n/a'
  let expSub = ''
  if (a.expiryText) expTxt = a.expiryText
  else if (a.expiryEpoch !== null) expTxt = new Date(a.expiryEpoch * 1000).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })
  if (a.dte !== null) expSub = a.dte.toFixed(1) + ' DTE'

  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-dim mt-0 mb-2">Market read{a.symbol ? ' — ' + a.symbol : ''}</div>
      <div className="flex flex-wrap gap-2.5">
        <Tile label="Spot" value={a.spot === null ? 'n/a' : a.spot.toLocaleString('en-IN')} sub={a.lot !== null ? 'lot ' + a.lot : ''} />
        <Tile label="Expiry" value={expTxt} sub={expSub} />
        <Tile label="ATM IV" value={pctIv(a.atmIv)} sub={a.ivPctile !== null ? 'pctile ' + a.ivPctile.toFixed(0) : ''} />
        <Tile label="PCR (OI)" value={a.pcr === null ? 'n/a' : a.pcr.toFixed(2)} sub={a.pcrFlag || ''} />
        <Tile label="Max pain" value={a.maxPain === null ? 'n/a' : String(a.maxPain)} />
        <Tile label="Put wall" value={a.putWall ? String(a.putWall.strike) : 'n/a'} sub={a.putWall?.oi != null ? 'OI ' + a.putWall.oi.toLocaleString('en-IN') : ''} />
        <Tile label="Call wall" value={a.callWall ? String(a.callWall.strike) : 'n/a'} sub={a.callWall?.oi != null ? 'OI ' + a.callWall.oi.toLocaleString('en-IN') : ''} />
        <BiasTile b={a.bias} />
        <Tile label="Vol view" value={a.volView || 'n/a'} />
        <Tile label="Range conviction" value={a.rangeConv === null ? 'n/a' : a.rangeConv.toFixed(2)} />
      </div>
      {a.provisional ? (
        <div className="text-amber text-xs my-2">Intraday OI is provisional right now (lagged ~3 min; true-up at 16:15) — treat reads as PROVISIONAL.</div>
      ) : null}
      {a.chainThin ? <div className="text-amber text-xs my-2">Thin chain: footprint confidences downgraded.</div> : null}
      {a.note ? <div className="text-amber text-xs my-2">{a.note}</div> : null}

      <div className="text-[10px] uppercase tracking-wider text-dim mt-4.5 mb-2">OI footprints</div>
      {a.footprints.length ? (
        a.footprints.map((fp: any, i: number) =>
          fp && fp.status && !fp.kind ? (
            <div key={i} className="flex items-baseline gap-2.5 flex-wrap bg-bg2 border border-line rounded-lg px-3 py-1.5 mb-1.5">
              <span className="text-[11.5px] text-dim flex-1 min-w-[200px]">
                {fp.status}
                {fp.reason ? ' — ' + fp.reason : ''}
              </span>
            </div>
          ) : (
            <div key={i} className="flex items-baseline gap-2.5 flex-wrap bg-bg2 border border-line rounded-lg px-3 py-1.5 mb-1.5">
              <span className="font-mono font-bold text-xs">{fp.kind || '?'}</span>
              <span className={`inline-block min-w-[44px] text-center font-mono text-[11px] font-semibold px-[7px] py-px rounded-[10px] ${confColor(fp.confidence)}`}>
                {fp.confidence || 'n/a'}
              </span>
              {fp.direction ? <span className="font-mono text-[11.5px]">{fp.direction}</span> : null}
              <span className="font-mono text-[11.5px]">{fmtStrikes(fp.strikes)}</span>
              <span className="text-[11.5px] text-dim flex-1 min-w-[200px]">{fp.evidence || fp.rationale || fp.reason || ''}</span>
            </div>
          ),
        )
      ) : (
        <Empty>No footprints detected (or insufficient history).</Empty>
      )}

      <div className="text-[10px] uppercase tracking-wider text-dim mt-4.5 mb-2">Generated structures (optimizer)</div>
      {a.generated.length ? (
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))' }}>
          {a.generated.map((s: any, i: number) => (
            <StratCard key={i} s={s} onSave={() => onSave('generated', i)} saved={savedKeys.has('generated' + i)} />
          ))}
        </div>
      ) : (
        <Empty>No generated candidate cleared the POP / reward-risk / max-loss floors.</Empty>
      )}
      <div className="text-[10px] uppercase tracking-wider text-dim mt-4.5 mb-2">Benchmark menu</div>
      {a.benchmarks.length ? (
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))' }}>
          {a.benchmarks.map((s: any, i: number) => (
            <StratCard key={i} s={s} onSave={() => onSave('benchmarks', i)} saved={savedKeys.has('benchmarks' + i)} />
          ))}
        </div>
      ) : (
        <Empty>No benchmark template fits the current regime.</Empty>
      )}
      {a.disclaimer || a.sizing ? (
        <div className="text-dim text-[11px] mt-3.5 max-w-[900px] leading-relaxed">
          {a.disclaimer}
          {a.disclaimer && a.sizing ? <br /> : null}
          {a.sizing}
        </div>
      ) : null}
    </div>
  )
}

/* ── zero-loss sub-panel ────────────────────────────────────────────── */

function zlRunning(st: any) {
  if (!st) return false
  if (st.running === true) return true
  const s = String(st.state || st.phase || '').toLowerCase()
  return s === 'running' || s === 'scanning' || s === 'started'
}
function zlFinished(st: any) {
  if (!st || zlRunning(st)) return false
  if (st.finished === true || st.done === true) return true
  const s = String(st.state || st.phase || '').toLowerCase()
  if (/done|finish|complete/.test(s)) return true
  const done = numOr(st.done !== undefined ? st.done : st.scanned)
  const total = numOr(st.total !== undefined ? st.total : st.universe)
  return total !== null && total > 0 && done !== null && done >= total
}
function zlFloor(s: any): number | null {
  for (const k of ['floor_per_lot', 'min_payoff_per_lot', 'worst_case_per_lot', 'floor_rupees', 'floor']) {
    if (s[k] !== undefined) {
      const v = numOr(s[k])
      if (v !== null) return v
    }
  }
  return null
}
function zlGroup(results: any[]) {
  const map = new Map<string, { symbol: string; spot: number | null; lot: number | null; structs: any[] }>()
  for (const r of results) {
    if (!r || typeof r !== 'object') continue
    const sym = String(r.symbol || r.sym || '?').toUpperCase()
    if (!map.has(sym)) map.set(sym, { symbol: sym, spot: null, lot: null, structs: [] })
    const g = map.get(sym)!
    if (g.spot === null) g.spot = numOr(r.spot)
    if (g.lot === null) g.lot = numOr(r.lot)
    const list = Array.isArray(r.structures) ? r.structures : Array.isArray(r.hits) ? r.hits : Array.isArray(r.candidates) ? r.candidates : [r]
    for (const s of list) if (s && typeof s === 'object') g.structs.push(s)
  }
  return Array.from(map.values())
}
function zlMinOi(s: any): number | null {
  const v = numOr(s.min_oi !== undefined ? s.min_oi : s.min_leg_oi)
  if (v !== null) return v
  let m: number | null = null
  for (const l of Array.isArray(s.legs) ? s.legs : []) {
    const o = numOr(l.oi)
    if (o !== null) m = m === null ? o : Math.min(m, o)
  }
  return m
}
function zlZone(s: any, floor: number | null): string {
  const z = s.profit_zone !== undefined ? s.profit_zone : s.zone
  if (typeof z === 'string' && z) return z
  if (Array.isArray(z) && z.length) return z.map((x: any) => (numOr(x) === null ? String(x) : Number(x).toFixed(1))).join(' – ')
  const bes = Array.isArray(s.breakevens) ? s.breakevens : []
  if (bes.length) return bes.map((b: number) => Number(b).toFixed(1)).join(' / ')
  return floor !== null && floor > 0 ? 'all prices' : 'n/a'
}
function zlFlowText(s: any): string {
  const nps = numOr(s.net_premium_per_share !== undefined ? s.net_premium_per_share : s.net_premium)
  let flow = s.net_flow || null
  if (!flow && nps !== null) flow = nps > 0 ? 'DEBIT' : 'CREDIT'
  if (nps !== null) return (flow || '') + ' ' + Math.abs(nps).toFixed(2) + '/sh'
  const cr = numOr(s.net_credit !== undefined ? s.net_credit : s.net_credit_per_lot)
  if (cr !== null) return (cr >= 0 ? 'CREDIT ' : 'DEBIT ') + inr(Math.abs(cr))
  return flow || 'n/a'
}
function FloorBadge({ f }: { f: number | null }) {
  if (f === null) return <span className="inline-block font-mono text-[10.5px] font-bold tracking-wide px-2 py-0.5 rounded text-dim bg-[rgba(124,135,151,.14)] border border-line whitespace-nowrap">FLOOR n/a</span>
  return (
    <span
      className={`inline-block font-mono text-[10.5px] font-bold tracking-wide px-2 py-0.5 rounded border whitespace-nowrap ${
        f >= 0 ? 'text-green bg-green-bg border-[rgba(47,191,113,.45)]' : 'text-red bg-red-bg border-[rgba(229,83,75,.45)]'
      }`}
    >
      FLOOR {f >= 0 ? '₹+' : '−₹'}
      {Math.abs(Math.round(f)).toLocaleString('en-IN')}
    </span>
  )
}
function ZlStruct({ s, onSave, saved }: { s: any; onSave: () => void; saved: boolean }) {
  const floor = zlFloor(s)
  const legs = Array.isArray(s.legs) ? s.legs : []
  const maxProfit = s.max_profit_per_lot === 'UNLIMITED' ? 'UNLIMITED' : inr(numOr(s.max_profit_per_lot !== undefined ? s.max_profit_per_lot : s.max_profit_rupees !== undefined ? s.max_profit_rupees : s.max_profit))
  const minOi = zlMinOi(s)
  const fr = numOr(s.friction_rupees !== undefined ? s.friction_rupees : s.friction)
  return (
    <div className="border-t border-line pt-2 flex flex-col gap-1.5">
      <div className="flex items-baseline gap-2 flex-wrap text-[12.5px] font-semibold">
        {s.label || s.name || 'structure'}
        <span className="text-[11px] font-mono text-dim font-normal">{zlFlowText(s)}</span>
        <FloorBadge f={floor} />
        {s.ltp_based ? (
          <span className="inline-block text-[10px] font-bold tracking-wide text-amber bg-[rgba(217,165,63,.15)] border border-[rgba(217,165,63,.5)] rounded px-1.5 py-0.5 whitespace-nowrap">LTP-BASED — VERIFY</span>
        ) : null}
        {legs.length ? (
          <button
            disabled={saved}
            onClick={onSave}
            className="ml-auto text-[10.5px] px-2 py-0.5 border border-line rounded bg-bg3 text-accent cursor-pointer hover:border-accent disabled:text-green disabled:border-green disabled:cursor-default"
          >
            {saved ? '✓ saved' : '💾 save'}
          </button>
        ) : null}
      </div>
      {legs.length ? (
        <table className="w-full border-collapse">
          <thead>
            <tr>
              {['Side', 'Type', 'Strike', 'Price (basis)'].map((h) => (
                <th key={h} className="text-[9.5px] uppercase tracking-wide text-faint text-left px-1.5 py-0.5 border-b border-line font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {legs.map((l: any, i: number) => {
              const raw = String(l.side !== undefined ? l.side : '?').toUpperCase()
              const buy = raw === 'BUY' || raw === '1' || raw === '+1' || l.side === 1
              const type = l.type || (l.is_call === true ? 'CE' : l.is_call === false ? 'PE' : '?')
              const px = numOr(l.premium !== undefined ? l.premium : l.price)
              let basis = l.basis || l.price_basis || l.price_src || l.src || l.quote || null
              if (!basis) basis = l.ltp_based || s.ltp_based ? 'ltp' : buy ? 'ask' : 'bid'
              return (
                <tr key={i} title={l.tsym || ''}>
                  <td className={`px-1.5 py-0.5 font-mono text-[11.5px] border-b border-[rgba(38,48,63,.5)] ${buy ? 'text-green font-bold' : 'text-red font-bold'}`}>{buy ? 'BUY' : 'SELL'}</td>
                  <td className="px-1.5 py-0.5 font-mono text-[11.5px] border-b border-[rgba(38,48,63,.5)]">{type}</td>
                  <td className="px-1.5 py-0.5 font-mono text-[11.5px] border-b border-[rgba(38,48,63,.5)]">{String(l.strike !== undefined ? l.strike : '?')}</td>
                  <td className="px-1.5 py-0.5 font-mono text-[11.5px] border-b border-[rgba(38,48,63,.5)]">
                    {px === null ? 'n/a' : px.toFixed(2)} <span className="text-faint">({String(basis).toLowerCase()})</span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : null}
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5">
        <span className="text-[10px] uppercase tracking-wide text-dim">Max profit / lot</span>
        <span className="font-mono text-xs text-green">{maxProfit}</span>
        <span className="text-[10px] uppercase tracking-wide text-dim">Floor / lot</span>
        <span className={`font-mono text-xs ${floor !== null && floor >= 0 ? 'text-green' : 'text-red'}`}>{floor === null ? 'n/a' : inr(floor)}</span>
        <span className="text-[10px] uppercase tracking-wide text-dim">Profit zone</span>
        <span className="font-mono text-xs">{zlZone(s, floor)}</span>
        <span className="text-[10px] uppercase tracking-wide text-dim">Min leg OI</span>
        <span className="font-mono text-xs">{minOi === null ? 'n/a' : minOi.toLocaleString('en-IN')}</span>
        {fr !== null ? (
          <>
            <span className="text-[10px] uppercase tracking-wide text-dim">Friction incl.</span>
            <span className="font-mono text-xs">{inr(fr)}</span>
          </>
        ) : null}
      </div>
    </div>
  )
}

function ZeroLossPanel() {
  const [status, setStatus] = useState<any>(null)
  const [groups, setGroups] = useState<{ symbol: string; spot: number | null; lot: number | null; structs: any[] }[]>([])
  const [limit, setLimit] = useState('')
  const [busy, setBusy] = useState(false)
  const [savedKeys, setSavedKeys] = useState<Set<string>>(new Set())
  const pollRef = useRef<number | null>(null)
  const universeLabel = useRef('228')

  async function refresh() {
    if (busy) return
    setBusy(true)
    try {
      const d = await jget<any>('/api/zeroloss')
      const st = d?.status || {}
      const results = Array.isArray(d?.results) ? d.results : []
      setStatus(st)
      const gs = zlGroup(results)
      gs.forEach((g) =>
        g.structs.sort((a, b) => {
          const fa = zlFloor(a)
          const fb = zlFloor(b)
          return (fb === null ? -1e15 : fb) - (fa === null ? -1e15 : fa)
        }),
      )
      setGroups(gs)
      if (zlRunning(st)) {
        if (!pollRef.current) pollRef.current = window.setInterval(refresh, 3000)
      } else if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    } catch {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
    setBusy(false)
  }

  useEffect(() => {
    refresh()
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function start() {
    if (zlRunning(status)) return
    setStatus({ running: true })
    const lim = parseInt(limit, 10)
    const payload: Record<string, number> = {}
    if (!Number.isNaN(lim) && lim > 0) payload.limit = lim
    try {
      await jpost('/api/zeroloss', payload)
      if (!pollRef.current) pollRef.current = window.setInterval(refresh, 3000)
      await refresh()
    } catch (e) {
      setStatus({ error: (e as Error).message })
    }
  }

  async function saveStruct(gi: number, si: number) {
    const g = groups[gi]
    const s = g?.structs?.[si]
    if (!g || !s || !Array.isArray(s.legs)) return
    const legs = s.legs.map((l: any) => {
      const raw = String(l.side !== undefined ? l.side : '').toUpperCase()
      const buy = raw === 'BUY' || raw === '1' || raw === '+1' || l.side === 1
      return { side: buy ? 'BUY' : 'SELL', type: l.type || (l.is_call === false ? 'PE' : 'CE'), strike: l.strike, premium: l.premium !== undefined ? l.premium : l.price, tsym: l.tsym || '' }
    })
    let nps = numOr(s.net_premium_per_share !== undefined ? s.net_premium_per_share : s.net_premium)
    if (nps === null) nps = legs.reduce((acc: number, l: any) => acc + (l.side === 'BUY' ? 1 : -1) * (numOr(l.premium) || 0), 0)
    const payload = {
      symbol: g.symbol,
      label: 'zero-loss: ' + (s.label || s.name || 'structure'),
      legs,
      net_premium_per_share: nps,
      lot: g.lot !== null ? g.lot : s.lot,
      metrics: { floor_per_lot: zlFloor(s), max_profit_per_lot: s.max_profit_per_lot, breakevens: s.breakevens, ltp_based: !!s.ltp_based },
    }
    try {
      await jpost('/api/strategist/save', payload)
      setSavedKeys((prev) => new Set(prev).add(gi + ':' + si))
    } catch {
      /* leave unsaved */
    }
  }

  const nres = groups.reduce((n, g) => n + g.structs.length, 0)
  const done = numOr(status?.done !== undefined ? status.done : status?.scanned)
  const total = numOr(status?.total !== undefined ? status.total : status?.universe)
  const hits = numOr(status?.hits !== undefined ? status.hits : status?.found !== undefined ? status.found : status?.matches)
  const n = hits === null ? nres : hits
  const frac = (done === null ? '…' : done) + '/' + (total === null ? '?' : total)

  return (
    <div>
      <div className="text-dim text-xs leading-relaxed max-w-[880px] bg-bg2 border border-line border-l-[3px] border-l-accent rounded-lg px-3.5 py-2.5 mb-3">
        Scans every F&amp;O chain for structures whose worst-case expiry payoff is &ge; 0 at EXECUTABLE bid/ask prices minus friction.
        <br />
        Screen prices move — treat hits as a shortlist to verify immediately, not free money.
      </div>
      <div className="flex items-center gap-2 mb-3.5">
        <button disabled={zlRunning(status)} onClick={start} className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent disabled:opacity-50">
          Scan all {universeLabel.current}
        </button>
        <input
          type="number"
          value={limit}
          onChange={(e) => setLimit(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && start()}
          placeholder="limit"
          min={1}
          step={1}
          title="Optional: scan only the first N symbols of the F&O universe"
          className="bg-bg text-fg border border-line rounded-md px-2 py-1.5 text-xs font-mono w-[84px]"
        />
        <span className="text-xs text-amber">
          {status?.error
            ? 'scan error: ' + status.error
            : zlRunning(status)
              ? (
                  <>
                    <Spin />
                    scanning {frac} — {n} hit{n === 1 ? '' : 's'}
                    {status?.symbol ? <> · <span className="num">{status.symbol}</span></> : null}
                  </>
                )
              : zlFinished(status)
                ? `scan finished — ${done === null ? '' : frac + ' chains, '}${n} hit${n === 1 ? '' : 's'}`
                : ''}
        </span>
      </div>
      <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))' }}>
        {groups.length ? (
          groups.map((g, gi) => (
            <div key={g.symbol} className="bg-bg2 border border-line rounded-[9px] p-3.5 flex flex-col gap-2">
              <div className="flex items-baseline gap-2.5 flex-wrap">
                <span className="font-mono font-bold text-[15px]">{g.symbol}</span>
                {g.spot !== null ? (
                  <span className="font-mono text-[11.5px] text-dim">
                    spot {g.spot.toLocaleString('en-IN')}
                    {g.lot !== null ? ' · lot ' + g.lot : ''}
                  </span>
                ) : null}
                <span className="text-[12.5px] flex-1 min-w-[120px]">{g.structs[0]?.label || g.structs[0]?.name || ''}</span>
                <FloorBadge f={zlFloor(g.structs[0] || {})} />
              </div>
              {g.structs.map((s, si) => (
                <ZlStruct key={si} s={s} onSave={() => saveStruct(gi, si)} saved={savedKeys.has(gi + ':' + si)} />
              ))}
            </div>
          ))
        ) : zlRunning(status) ? (
          <Empty>Scanning — no hits yet…</Empty>
        ) : zlFinished(status) ? (
          <Empty>No zero-loss structures exist at executable prices right now — that is the normal state of an efficient market; check again when chains are volatile.</Empty>
        ) : (
          <Empty>No scan yet — hit Scan to sweep every F&amp;O chain for mispriced zero-floor structures.</Empty>
        )}
      </div>
    </div>
  )
}

/* ── watchlist sub-panel ────────────────────────────────────────────── */

function WatchlistPanel() {
  const [saved, setSaved] = useState<{ count: number; total_pnl_per_lot?: number; items: any[] } | null>(null)
  const [watch, setWatch] = useState<{ strategy: string; count: number; avg_change_pct: number; win_rate: number; items: any[] }[] | null>(null)

  async function refreshSaved() {
    try {
      setSaved(await jget('/api/strategist/saved'))
    } catch {
      setSaved(null)
    }
  }
  async function refreshWatch() {
    try {
      const j = await jget<{ strategies: any[] }>('/api/watchlist')
      setWatch(j.strategies || [])
    } catch {
      setWatch(null)
    }
  }
  useEffect(() => {
    refreshSaved()
    refreshWatch()
  }, [])

  async function removeSaved(id: string) {
    try {
      await jpost('/api/strategist/save/remove', { id })
      refreshSaved()
    } catch {
      /* ignore */
    }
  }
  async function removeWatch(id: string) {
    await fetch('/api/watchlist/remove', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) })
    refreshWatch()
  }

  const items = saved?.items || []
  return (
    <div>
      <div className="font-bold mb-2.5 flex items-center gap-2.5">
        Saved strategies — live mark-to-market P&amp;L
        <span className="text-dim font-normal">{saved?.count ? `${saved.count} saved${saved.total_pnl_per_lot != null ? ' · net ' + inr(saved.total_pnl_per_lot) + '/lot' : ''}` : ''}</span>
        <button onClick={refreshSaved} className="text-xs px-2 py-0.5 bg-bg3 border border-line rounded hover:border-accent hover:text-accent">
          ↻
        </button>
      </div>
      {items.length ? (
        items.map((it) => {
          const pnl = numOr(it.pnl_per_lot)
          const cls = pnl === null ? 'text-dim' : pnl >= 0 ? 'text-green' : 'text-red'
          const pnlTxt = pnl === null ? 'pricing n/a (chain closed?)' : inr(pnl) + '/lot'
          const ps = numOr(it.pnl_per_share)
          return (
            <div key={it.id} className="border border-line rounded-lg px-3 py-2.5 my-2 bg-bg2">
              <div className="flex items-center gap-2 text-[13px]">
                <b>{it.symbol}</b> · {it.label}
                <span className={`ml-auto font-mono font-semibold ${cls}`}>{pnlTxt}</span>
                <button onClick={() => removeSaved(it.id)} title="remove" className="text-xs px-2 py-0.5 bg-bg3 border border-line rounded hover:border-accent hover:text-accent">
                  ✕
                </button>
              </div>
              <div className="text-[11.5px] my-1">
                {(it.legs || [])
                  .map((l: any) => `${l.side} ${l.opt_type} ${l.strike}`)
                  .join(' · ')}
              </div>
              <div className="text-dim text-[11px]">
                entry {it.entry_net_per_share != null ? it.entry_net_per_share : '—'}/sh · now {it.current_net_per_share != null ? it.current_net_per_share : '—'}/sh
                {ps != null ? ` · ${ps >= 0 ? '+' : ''}${ps}/sh` : ''} · saved {it.saved_at || ''}
              </div>
            </div>
          )
        })
      ) : (
        <div className="text-dim">No saved strategies yet — hit “💾 save” on a structure in the Advisor.</div>
      )}

      <div className="font-bold mt-6 mb-2.5 flex items-center gap-2.5">
        Saved stocks — P&amp;L since you added <span className="text-dim font-normal">(per strategy)</span>
        <button onClick={refreshWatch} className="text-xs px-2 py-0.5 bg-bg3 border border-line rounded hover:border-accent hover:text-accent">
          ↻
        </button>
      </div>
      {watch?.length ? (
        watch.map((g) => (
          <div key={g.strategy} className="my-2 mb-4">
            <h4 className="mb-1.5 flex gap-3 items-baseline">
              {g.strategy}{' '}
              <span className="text-dim font-normal text-xs">
                {g.count} items · avg <span className={g.avg_change_pct > 0 ? 'text-green' : g.avg_change_pct < 0 ? 'text-red' : ''}>{g.avg_change_pct?.toFixed?.(1)}%</span> · win {g.win_rate?.toFixed?.(0)}%
              </span>
            </h4>
            <div className="overflow-x-auto">
              <table className="border-collapse w-full text-xs">
                <thead>
                  <tr>
                    {['Symbol', 'Seg', 'Dir', 'Added', 'Add px', 'Now', 'Since add', ''].map((h) => (
                      <th key={h} className="text-left px-2.5 py-1 border-b border-line text-dim font-semibold whitespace-nowrap">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(g.items || []).map((i: any) => (
                    <tr key={i.id}>
                      <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">
                        <b>{i.symbol}</b>
                      </td>
                      <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{i.segment || ''}</td>
                      <td className={`px-2.5 py-1 border-b border-line whitespace-nowrap ${i.direction === 'BUY' ? 'text-green' : 'text-red'}`}>{i.direction}</td>
                      <td className="px-2.5 py-1 border-b border-line whitespace-nowrap text-dim">{(i.added_at || '').replace('T', ' ')}</td>
                      <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{i.price_at_add}</td>
                      <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">{i.pnl ? i.pnl.current : '–'}</td>
                      <td className={`px-2.5 py-1 border-b border-line whitespace-nowrap ${i.pnl ? (i.pnl.change_pct > 0 ? 'text-green' : i.pnl.change_pct < 0 ? 'text-red' : '') : ''}`}>
                        {i.pnl ? i.pnl.change_pct + '%' : '–'}
                      </td>
                      <td className="px-2.5 py-1 border-b border-line whitespace-nowrap">
                        <button onClick={() => removeWatch(i.id)} className="text-[11px] px-2 py-0.5 bg-bg3 border border-line rounded hover:border-accent hover:text-accent">
                          ✕
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))
      ) : (
        <div className="text-dim">Watchlist empty — add hits with the “+ watch” button on the Swing tab.</div>
      )}
    </div>
  )
}

/* ── container ───────────────────────────────────────────────────────── */

type SubTab = 'advisor' | 'zeroloss' | 'watch'

export function AdvisorView({ visible }: { visible: boolean }) {
  const [sub, setSub] = useState<SubTab>('advisor')
  const [, setModel] = useState<any>(null)

  return (
    <div className={`flex-1 min-h-0 overflow-y-auto px-4.5 py-3.5 pb-10 ${visible ? '' : 'hidden'}`}>
      <nav className="flex gap-1.5 mb-3.5">
        {(['advisor', 'zeroloss', 'watch'] as SubTab[]).map((t) => (
          <button
            key={t}
            onClick={() => setSub(t)}
            className={`bg-transparent border rounded-full px-3.5 py-1 text-xs ${
              sub === t ? 'bg-bg3 text-accent border-accent' : 'text-dim border-line hover:text-accent hover:border-accent'
            }`}
          >
            {t === 'advisor' ? 'Advisor' : t === 'zeroloss' ? 'Zero-Loss Search' : 'Watchlist'}
          </button>
        ))}
      </nav>
      {sub === 'advisor' ? <AdvisorPanel onModelUpdate={setModel} /> : null}
      {sub === 'zeroloss' ? <ZeroLossPanel /> : null}
      {sub === 'watch' ? <WatchlistPanel /> : null}
    </div>
  )
}

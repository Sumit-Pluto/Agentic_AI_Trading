import { useEffect, useRef, useState } from 'react'
import { jget } from '../lib/api'
import { numOr } from '../lib/format'
import { Empty } from '../components/ui'

const NEWS_SECTORS = [
  'BANKING', 'IT', 'PHARMA', 'AUTO', 'METAL', 'ENERGY', 'FMCG', 'REALTY', 'INFRA', 'FINANCE', 'TELECOM', 'CEMENT', 'POWER', 'DEFENCE', 'CHEMICALS',
]
const TAPE_CHIPS: [string, string][] = [
  ['gift_nifty', 'GIFT Nifty'], ['spx_fut', 'S&P Fut'], ['nasdaq_fut', 'Nasdaq Fut'], ['dow_fut', 'Dow Fut'], ['usdinr', 'USD/INR'],
  ['crude_brent', 'Brent'], ['us10y', 'US10Y'], ['spx', 'SPX'], ['nikkei', 'Nikkei'], ['hangseng', 'HangSeng'], ['gold', 'Gold'],
]

function ageTxt(m: unknown) {
  const n = numOr(m)
  if (n === null) return ''
  if (n < 60) return Math.round(n) + 'm'
  if (n < 1440) return (n / 60).toFixed(1) + 'h'
  return Math.round(n / 1440) + 'd'
}

function NewsLink({ it }: { it: any }) {
  const href = String(it.link || '')
  const title = it.title || '(untitled)'
  if (/^https?:\/\//i.test(href))
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" className="text-fg no-underline text-[12.5px] leading-tight hover:text-accent">
        {title}
      </a>
    )
  return <span className="text-fg text-[12.5px] leading-tight">{title}</span>
}

function NewsItem({ it }: { it: any }) {
  const meta = [it.source || '?', ageTxt(it.age_min)].filter(Boolean).join(' · ')
  return (
    <div className="py-1.5">
      <NewsLink it={it} />
      <div className="text-[10.5px] text-faint mt-0.5 font-mono">{meta}</div>
    </div>
  )
}

function NewsList({ items, emptyMsg }: { items: any[]; emptyMsg: string }) {
  if (!items?.length) return <Empty>{emptyMsg}</Empty>
  return <>{items.map((it, i) => <NewsItem key={i} it={it} />)}</>
}

function WeightedItem({ it }: { it: any }) {
  const w = numOr(it.weight)
  const tier = w === null ? 'cool' : w >= 60 ? 'hot' : w >= 35 ? 'warm' : 'cool'
  const tierCls = tier === 'hot' ? 'text-white bg-red' : tier === 'warm' ? 'text-white bg-amber' : 'text-dim bg-bg3 border border-line'
  const tags = (Array.isArray(it.reasons) ? it.reasons : []).slice(0, 6)
  const meta = [it.source || '?', ageTxt(it.age_min)].filter(Boolean).join(' · ')
  return (
    <div className="py-1.5">
      <span className={`inline-block min-w-[36px] text-center align-[1px] font-mono font-bold text-[11px] rounded px-1.5 mr-1.5 ${tierCls}`}>
        {w === null ? '–' : Math.round(w)}
      </span>
      <NewsLink it={it} />
      <div className="text-[10.5px] text-faint mt-0.5 font-mono">{meta}</div>
      {tags.length ? (
        <div>
          {tags.map((r: string, i: number) => (
            <span key={i} className="inline-block text-[9.5px] text-dim bg-[rgba(124,135,151,.12)] rounded-sm px-1.5 mt-0.5 mr-1">
              {r}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function EconEvent({ e, stars }: { e: any; stars: boolean }) {
  const imp = Math.max(1, Math.min(3, Math.round(numOr(e.impact) || 1)))
  const cc = String(e.country || '').trim().toUpperCase()
  const isIn = cc === 'IN' || cc === 'IND' || cc === 'INDIA'
  const when = (e.date || '?') + (e.time ? ' ' + e.time : '')
  const dotColors = ['', 'bg-faint', 'bg-amber', 'bg-red']
  return (
    <div className="py-1 text-[11.5px] border-b border-[rgba(38,48,63,.4)]">
      <span className={`inline-block min-w-[26px] text-center font-mono text-[9.5px] font-bold rounded px-1.5 mr-1.5 align-[1px] border ${isIn ? 'text-green bg-green-bg border-[rgba(47,191,113,.35)]' : 'text-dim bg-[rgba(124,135,151,.14)] border-line'}`}>
        {cc.slice(0, 3) || '?'}
      </span>
      {!stars ? <span className={`inline-block w-[7px] h-[7px] rounded-full mr-1.5 align-[1px] ${dotColors[imp]}`} /> : null}
      <span className="font-mono text-faint mr-1.5">{when}</span>
      {e.event || '?'}
      {stars ? <span className="text-amber tracking-widest text-[11px] ml-1.5">{'★'.repeat(imp)}</span> : null}
    </div>
  )
}

function CorpAccordion({ corp }: { corp: any }) {
  const buckets: [string, string][] = [['ipo', 'IPO'], ['results', 'Results'], ['dividends', 'Dividends'], ['actions', 'Actions']]
  return (
    <>
      {buckets.map(([key, label]) => {
        const rows = corp && Array.isArray(corp[key]) ? corp[key] : []
        return (
          <details key={key} className="border border-line rounded-md my-1.5 bg-bg3" open={!!rows.length && key === 'results'}>
            <summary className="cursor-pointer px-2.5 py-1.5 text-xs select-none">
              {label} <span className="text-faint">({rows.length})</span>
            </summary>
            <div className="px-2.5 pb-2">
              {rows.length ? (
                rows.slice(0, 40).map((r: any, i: number) => (
                  <div key={i} className="py-1 text-[11.5px] border-b border-[rgba(38,48,63,.4)]">
                    <span className="font-mono text-faint mr-1.5">{r.date || '?'}</span>
                    <b>{r.symbol || '?'}</b> <span className="text-dim">{r.detail || r.purpose || ''}</span>
                  </div>
                ))
              ) : (
                <div className="text-faint py-1">nothing scheduled</div>
              )}
            </div>
          </details>
        )
      })}
    </>
  )
}

export function NewsView({ visible }: { visible: boolean }) {
  const [d, setD] = useState<any>(null)
  const [inTab, setInTab] = useState<'all' | 'sectors' | 'cal'>('all')
  const [sector, setSector] = useState('')
  const busyRef = useRef(false)

  async function refresh() {
    if (busyRef.current) return
    busyRef.current = true
    try {
      setD(await jget('/api/news'))
    } catch {
      /* best-effort */
    }
    busyRef.current = false
  }

  useEffect(() => {
    if (visible) refresh()
  }, [visible])

  useEffect(() => {
    const id = setInterval(() => {
      if (visible && document.visibilityState !== 'hidden') refresh()
    }, 60000)
    return () => clearInterval(id)
  }, [visible])

  if (!d?.generated_at) {
    return (
      <div className={`flex-1 min-h-0 flex gap-3 px-3.5 py-3 ${visible ? '' : 'hidden'}`}>
        <Empty>News hub warming up — first refresh pending…</Empty>
      </div>
    )
  }

  const health = d.health || {}
  const down = Object.keys(health).filter((k) => health[k] && health[k].ok === false)
  const indian = Array.isArray(d.indian_news) ? d.indian_news : []
  const filtered = sector ? indian.filter((it: any) => Array.isArray(it.sectors) && it.sectors.includes(sector)) : indian.filter((it: any) => Array.isArray(it.sectors) && it.sectors.length)
  const cals = d.calendars || {}
  const macro = Array.isArray(cals.macro) ? cals.macro : []
  const tape = d.market_tape || {}
  const f = d.fii_dii
  const rs = numOr(d.risk_score)
  const weighted = Array.isArray(d.weighted) ? d.weighted : []
  const glob = Array.isArray(d.global_news) ? d.global_news : []
  const gcal = Array.isArray(cals.global) ? cals.global : []

  return (
    <div className={`flex-1 min-h-0 flex gap-3 px-3.5 py-3 ${visible ? '' : 'hidden'}`}>
      <div className="w-[330px] shrink-0 flex flex-col min-h-0">
        <div className="bg-bg2 border border-line rounded-[10px] flex flex-col flex-1 min-h-0">
          <div className="flex items-center gap-2.5 shrink-0 px-3 pt-2.5 pb-2 border-b border-line">
            <span className="text-[11px] uppercase tracking-wider text-dim font-semibold whitespace-nowrap">Indian Market</span>
            <nav className="flex gap-1 ml-auto">
              {(['all', 'sectors', 'cal'] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setInTab(t)}
                  className={`px-2 py-0.5 text-[11px] rounded ${inTab === t ? 'bg-bg3 text-accent border border-line' : 'text-dim border border-transparent hover:text-fg hover:border-line'}`}
                >
                  {t === 'all' ? 'All' : t === 'sectors' ? 'Sectors' : 'Calendar'}
                </button>
              ))}
            </nav>
          </div>
          <div className="flex-1 overflow-y-auto min-h-0 px-3 pt-2 pb-4">
            {inTab === 'all' ? <NewsList items={indian.slice(0, 60)} emptyMsg="No Indian market news yet." /> : null}
            {inTab === 'sectors' ? (
              <>
                <select value={sector} onChange={(e) => setSector(e.target.value)} className="w-full bg-bg text-fg border border-line rounded-md px-2 py-1.5 text-xs my-1 mb-2">
                  <option value="">All sectors (any tag)</option>
                  {NEWS_SECTORS.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
                <NewsList items={filtered.slice(0, 60)} emptyMsg={sector ? `No tagged ${sector} news right now.` : 'No sector-tagged news right now.'} />
              </>
            ) : null}
            {inTab === 'cal' ? (
              <>
                <div className="text-[10px] uppercase tracking-wider text-dim my-1.5">Macro events (IN + US)</div>
                {macro.length ? macro.slice(0, 40).map((e: any, i: number) => <EconEvent key={i} e={e} stars={false} />) : <div className="text-faint py-1">no events</div>}
                <div className="text-[10px] uppercase tracking-wider text-dim my-1.5">Corporate</div>
                <CorpAccordion corp={cals.corporate} />
              </>
            ) : null}
          </div>
        </div>
      </div>

      <div className="flex-1 min-w-0 flex flex-col min-h-0">
        <div className="bg-bg2 border border-line rounded-[10px] flex flex-col flex-1 min-h-0">
          <div className="flex items-center gap-2.5 shrink-0 px-3 pt-2.5 pb-2 border-b border-line">
            <span className="text-[11px] uppercase tracking-wider text-dim font-semibold whitespace-nowrap">Most Weighted News</span>
            {down.length ? (
              <span className="text-[10.5px] text-amber ml-auto max-w-[55%] overflow-hidden text-ellipsis whitespace-nowrap" title={down.map((k) => `${k} — ${health[k]?.error || '?'}`).join('\n')}>
                {down.length} source{down.length > 1 ? 's' : ''} down: {down.join(', ')}
              </span>
            ) : null}
          </div>
          <div className="flex-1 overflow-y-auto min-h-0 px-3 pt-2 pb-4">
            <div className="flex flex-wrap gap-1.5 items-center pb-2 border-b border-line mb-2">
              {TAPE_CHIPS.map(([key, label]) => {
                const q = tape[key]
                const price = q ? numOr(q.price) : null
                const c = q ? numOr(q.chg_pct) : null
                return (
                  <span key={key} className="bg-bg3 border border-line rounded-md px-2 py-1 font-mono text-[11px] whitespace-nowrap">
                    <span className="text-dim text-[9.5px] uppercase tracking-wide mr-1">{label}</span>
                    {price === null ? (
                      <span className="text-faint">n/a</span>
                    ) : (
                      <>
                        {price.toLocaleString('en-IN', { maximumFractionDigits: 2 })}{' '}
                        {c === null ? '' : <span className={c >= 0 ? 'text-green' : 'text-red'}>{(c >= 0 ? '+' : '') + c.toFixed(2)}%</span>}
                      </>
                    )}
                  </span>
                )
              })}
              {f && (numOr(f.fii_net_cr) !== null || numOr(f.dii_net_cr) !== null) ? (
                <span title={f.date || ''} className="bg-bg3 border border-line rounded-md px-2 py-1 font-mono text-[11px] whitespace-nowrap">
                  <span className="text-dim text-[9.5px] uppercase tracking-wide mr-1">FII/DII</span>
                  {numOr(f.fii_net_cr) === null ? 'n/a' : <span className={f.fii_net_cr >= 0 ? 'text-green' : 'text-red'}>{(f.fii_net_cr >= 0 ? '+' : '') + Math.round(f.fii_net_cr).toLocaleString('en-IN')}</span>}
                  {' / '}
                  {numOr(f.dii_net_cr) === null ? 'n/a' : <span className={f.dii_net_cr >= 0 ? 'text-green' : 'text-red'}>{(f.dii_net_cr >= 0 ? '+' : '') + Math.round(f.dii_net_cr).toLocaleString('en-IN')}</span>}
                  <span className="text-faint"> cr</span>
                </span>
              ) : (
                <span className="bg-bg3 border border-line rounded-md px-2 py-1 font-mono text-[11px] whitespace-nowrap">
                  <span className="text-dim text-[9.5px] uppercase tracking-wide mr-1">FII/DII</span>
                  <span className="text-faint">n/a</span>
                </span>
              )}
            </div>
            <div className="flex items-center gap-2.5 my-0.5 mb-3">
              <span className="text-[10px] uppercase tracking-wide text-dim whitespace-nowrap">Risk meter</span>
              <div className="relative flex-1 h-2 rounded-md opacity-90" style={{ background: 'linear-gradient(90deg, var(--red), var(--amber), var(--green))' }}>
                <div className="absolute -top-[3px] w-[3px] h-3.5 bg-white rounded-sm shadow" style={{ left: rs === null ? '50%' : `${((rs + 1) / 2) * 100}%` }} />
              </div>
              <span className={`font-mono text-[11px] whitespace-nowrap ${rs === null ? '' : rs >= 0.25 ? 'text-green' : rs <= -0.25 ? 'text-red' : ''}`}>
                {rs === null ? 'n/a' : `${(rs >= 0 ? '+' : '') + rs.toFixed(2)} ${rs >= 0.25 ? 'risk-on' : rs <= -0.25 ? 'risk-off' : 'neutral'}`}
              </span>
            </div>
            {weighted.length ? weighted.map((it: any, i: number) => <WeightedItem key={i} it={it} />) : <Empty>No weighted news yet.</Empty>}
          </div>
        </div>
      </div>

      <div className="w-[330px] shrink-0 flex flex-col min-h-0">
        <div className="bg-bg2 border border-line rounded-[10px] flex flex-col flex-1 min-h-0">
          <div className="flex items-center gap-2.5 shrink-0 px-3 pt-2.5 pb-2 border-b border-line">
            <span className="text-[11px] uppercase tracking-wider text-dim font-semibold whitespace-nowrap">Global Market</span>
          </div>
          <div className="flex-1 overflow-y-auto min-h-0 px-3 pt-2 pb-4">
            <NewsList items={glob.slice(0, 40)} emptyMsg="No global news yet." />
            <div className="text-[10px] uppercase tracking-wider text-dim my-1.5">
              Global Calendar <span className="normal-case tracking-normal">(non-IN · ★ = impact)</span>
            </div>
            {gcal.length ? gcal.slice(0, 30).map((e: any, i: number) => <EconEvent key={i} e={e} stars /> ) : <div className="text-faint py-1">no high-impact events</div>}
          </div>
        </div>
      </div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { apiGet, inr, type Budget, type FundsInfo } from '../lib/live'

export function FundsView() {
  const [funds, setFunds] = useState<FundsInfo | null>(null)
  const [budget, setBudget] = useState<Budget | null>(null)
  useEffect(() => {
    const tick = () => {
      apiGet<{ funds: FundsInfo | null }>('/api/funds').then((d) => setFunds(d.funds)).catch(() => {})
      apiGet<{ budget: Budget }>('/api/budget').then((d) => setBudget(d.budget)).catch(() => {})
    }
    tick(); const h = setInterval(tick, 5000); return () => clearInterval(h)
  }, [])

  if (!funds) {
    return <div className="card" style={{ padding: 16 }}><span className="dim">
      No Gateway client on this server — funds need engine_mode=live with broker credentials.</span></div>
  }
  const equity = funds.cash + funds.margin_used
  const avail = Math.max(equity - funds.margin_used, 0)
  const util = equity > 0 ? (funds.margin_used / equity) * 100 : 0
  const utilColor = util >= 90 ? 'var(--red)' : util >= 60 ? 'var(--amber)' : 'var(--green)'
  const tiles = [
    { k: 'Account Cash', v: inr(funds.cash) },
    { k: "Today's Payin", v: inr(funds.payin) },
    { k: 'Collateral', v: inr(funds.collateral) },
    { k: 'Margin Used', v: inr(funds.margin_used) },
    { k: 'Margin Available', v: inr(avail) },
    { k: 'Engine Budget', v: inr(budget?.total), t: 'self-cap this system may deploy of the account' },
    { k: 'Engine Deployed', v: inr(budget?.deployed) },
    { k: 'Engine Cap', v: (budget?.cap_state || 'OK').replace('_', ' ') },
  ]
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, height: '100%', minHeight: 0 }}>
      <div className="tiles tiles-4">
        {tiles.map((t) => (
          <div key={t.k} className="card tile" title={t.t ?? t.k}>
            <div className="faint k">{t.k}</div>
            <div className="mono v">{t.v}</div>
          </div>
        ))}
      </div>
      <div className="card" style={{ padding: 14 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
          <span className="faint" style={{ fontSize: 11, textTransform: 'uppercase' }}>Margin utilisation</span>
          <span className="mono" style={{ color: utilColor, fontWeight: 700 }}>{util.toFixed(1)}%</span>
        </div>
        <div style={{ height: 10, background: 'var(--bg3)', borderRadius: 5, overflow: 'hidden' }}>
          <div style={{ height: '100%', width: `${Math.min(100, util)}%`, background: utilColor }} />
        </div>
        <div className="dim" style={{ fontSize: 11, marginTop: 8 }}>
          Entries block above the configured margin cap; the engine budget bounds this system's share of the account.
        </div>
      </div>
    </div>
  )
}

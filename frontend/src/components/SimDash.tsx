import { useState } from 'react'
import { fmt, hhmmss, numOr, signed } from '../lib/format'
import { Badge, Chip } from './ui'

export interface SimTrade {
  symbol: string
  direction: string
  entry_time: string
  exit_time: string
  entry: number
  exit: number
  exit_reason: string
  pnl_per_share: number
  score: number
}

export interface SimStatus {
  running?: boolean
  error?: string
  day?: string
  done?: number
  total?: number
  found?: number
  stats?: {
    trades: number
    win_rate: number
    wins: number
    losses: number
    pnl_sum_per_share: number
    avg_win: number
    avg_loss: number
    best: number
    worst: number
  }
  trades?: SimTrade[]
}

function reasonTone(r: string): 'ok' | 'bad' | 'na' {
  if (r === 'stop') return 'bad'
  if (r === 'target1' || r === 'target2') return 'ok'
  return 'na'
}

export function SimDash({ status, onRowClick }: { status: SimStatus | null; onRowClick: (t: SimTrade) => void }) {
  const [hidden, setHidden] = useState(false)
  const stats = status?.stats
  const trades = status?.trades || []
  const n = stats?.trades || 0
  if (!status || status.running || !n) return null

  const pnl = numOr(stats?.pnl_sum_per_share)

  return (
    <div className="shrink-0 bg-bg2 border-b border-line">
      <div className="flex items-center gap-5 flex-wrap px-4 pt-1.5 pb-1">
        <span className="text-[10px] uppercase tracking-wider text-dim">Sim results</span>
        <span className="text-[11px] text-dim font-mono">{status.day ? '· ' + status.day : ''}</span>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Trades</span>
          <span className="font-mono text-[13px]">{n}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Win rate</span>
          <span className="font-mono text-[13px]">
            {stats && numOr(stats.win_rate) !== null ? `${stats.win_rate.toFixed(1)}% (${numOr(stats.wins) || 0}W/${numOr(stats.losses) || 0}L)` : '–'}
          </span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Total P&amp;L/sh</span>
          <span className={`font-mono text-[13px] ${pnl === null ? '' : pnl >= 0 ? 'text-green' : 'text-red'}`}>{signed(pnl)}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Avg win</span>
          <span className="font-mono text-[13px]">{signed(stats?.avg_win)}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Avg loss</span>
          <span className="font-mono text-[13px]">{signed(stats?.avg_loss)}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Best</span>
          <span className="font-mono text-[13px]">{signed(stats?.best)}</span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] uppercase tracking-wide text-dim">Worst</span>
          <span className="font-mono text-[13px]">{signed(stats?.worst)}</span>
        </div>
        <button
          onClick={() => setHidden((v) => !v)}
          className="ml-auto bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent"
        >
          {hidden ? 'Show trades' : 'Hide trades'}
        </button>
      </div>
      {!hidden ? (
        <div className="max-h-[168px] overflow-auto border-t border-[rgba(38,48,63,.5)]">
          <table className="w-full border-collapse">
            <thead>
              <tr>
                {['Symbol', 'Side', 'Entry → Exit', 'Entry', 'Exit', 'Reason', 'P&L/sh', 'Score'].map((h) => (
                  <th key={h} className="sticky top-0 bg-bg2 z-10 text-[10px] uppercase tracking-wide text-faint text-left px-2.5 py-0.5 border-b border-line font-medium">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {trades.map((t, i) => {
                const p = numOr(t.pnl_per_share)
                return (
                  <tr key={i} className="cursor-pointer hover:bg-bg3" onClick={() => onRowClick(t)}>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                      <b>{t.symbol}</b>
                    </td>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                      <Badge direction={t.direction} />
                    </td>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                      {hhmmss(t.entry_time)} → {hhmmss(t.exit_time)}
                    </td>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">{fmt(t.entry, 2)}</td>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">{fmt(t.exit, 2)}</td>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">
                      <Chip tone={reasonTone(t.exit_reason)}>{t.exit_reason ?? '?'}</Chip>
                    </td>
                    <td className={`px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px] ${p === null ? '' : p < 0 ? 'text-red' : 'text-green'}`}>
                      {signed(p)}
                    </td>
                    <td className="px-2.5 py-1 border-b border-[rgba(38,48,63,.5)] whitespace-nowrap font-mono text-[11.5px]">{fmt(t.score, 1)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  )
}

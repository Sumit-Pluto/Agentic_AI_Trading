import type { ReactNode } from 'react'

export function Chip({
  tone = 'na',
  children,
  title,
  className = '',
}: {
  tone?: 'ok' | 'bad' | 'na' | 'openpos'
  children: ReactNode
  title?: string
  className?: string
}) {
  const tones: Record<string, string> = {
    ok: 'text-green bg-green-bg',
    bad: 'text-red bg-red-bg',
    na: 'text-dim bg-[rgba(124,135,151,.14)]',
    openpos: 'text-accent bg-[rgba(76,141,255,.14)]',
  }
  return (
    <span
      title={title}
      className={`inline-block min-w-[44px] text-center font-mono text-[11px] font-semibold px-[7px] py-px rounded-[10px] ${tones[tone]} ${className}`}
    >
      {children}
    </span>
  )
}

export function Badge({ direction }: { direction?: string | null }) {
  const buy = direction !== 'SELL'
  return (
    <span
      className={`inline-block font-mono text-[10px] font-bold px-[7px] py-px rounded tracking-wide ${
        buy ? 'text-green bg-green-bg' : 'text-red bg-red-bg'
      }`}
    >
      {direction ?? '?'}
    </span>
  )
}

export function Tile({ label, value, sub, valueClass = '' }: { label: string; value: ReactNode; sub?: ReactNode; valueClass?: string }) {
  return (
    <div className="bg-bg2 border border-line rounded-lg px-3 py-2 min-w-[96px]">
      <span className="block text-[10px] uppercase tracking-wide text-dim">{label}</span>
      <span className={`block font-mono text-sm mt-0.5 whitespace-nowrap ${valueClass}`}>{value}</span>
      {sub ? <span className="block text-[10px] text-faint font-mono">{sub}</span> : null}
    </div>
  )
}

export function StatItem({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-wide text-dim">{label}</span>
      <span className="font-mono text-[13px]">{value}</span>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="text-faint px-3.5 py-4.5 text-xs">{children}</div>
}

export function Switch({ checked, onChange, disabled }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className="relative inline-block w-[30px] h-4 shrink-0" onClick={(e) => e.stopPropagation()}>
      <input
        type="checkbox"
        className="opacity-0 w-0 h-0 peer"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span
        className={`absolute inset-0 rounded-2xl transition-colors cursor-pointer ${checked ? 'bg-green' : 'bg-faint'} ${disabled ? 'opacity-60' : ''}`}
      >
        <span
          className={`absolute w-3 h-3 left-0.5 top-0.5 rounded-full bg-bg transition-transform ${checked ? 'translate-x-3.5' : ''}`}
        />
      </span>
    </label>
  )
}

export function Spin() {
  return <span className="spin" />
}

export function PanelTitle({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`text-[10px] uppercase tracking-wider text-dim px-3.5 pt-2.5 pb-1.5 ${className}`}>{children}</div>
}

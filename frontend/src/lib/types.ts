export interface TreeNode {
  key: string
  name: string
  score: number | null
  passed: boolean
  enabled: boolean
  weight: number
  threshold: number
  detail?: string
  children?: TreeNode[]
}

export interface Trigger {
  bar_time?: string
  armed_by?: { label: string; bar_time: string }
}

export interface VetoEntry {
  key: string
  score: number
  floor: number
}

export interface EvalResult {
  symbol: string
  direction: 'BUY' | 'SELL'
  score: number | null
  threshold: number
  accepted: boolean
  vetoed_by?: VetoEntry[] | null
  tree: TreeNode
  trigger?: Trigger | null
  evaluated_at: string
  price?: number
  sim?: boolean
}

export interface StateData {
  universe: number | string
  last_sweep: string | null
  sweep_seconds: number
  paper_count: number
  signal_threshold: number
  signals: EvalResult[]
}

export interface Position {
  symbol: string
  direction: string
  entry_price: number
  stop: number
  target1: number
  state: string
  unrealized_per_share: number | null
}

export interface PositionsResponse {
  positions: Position[]
  open: number
  partial: number
  realized_pnl_today_per_share_weighted: number | null
  closed_today: number | null
}

export interface ActivityItem {
  time: string
  kind?: string
  text?: string
}

export interface ModeState {
  mode: 'paper' | 'live'
  live: boolean
  executor: {
    day?: { realized: number; orders: number }
    limits?: Record<string, number>
    open_live?: number
  } | null
  live_open?: number
}

export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

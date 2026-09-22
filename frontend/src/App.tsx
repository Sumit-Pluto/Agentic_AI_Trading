import { useEffect, useRef, useState } from 'react'
import { AssistantWidget } from './components/AssistantWidget'
import { Header, type TabName } from './components/Header'
import { LiveTradingModal } from './components/LiveTradingModal'
import { SimDash, type SimStatus, type SimTrade } from './components/SimDash'
import { useVisiblePolling } from './hooks/usePolling'
import { jget, jpost } from './lib/api'
import type { ModeState, StateData } from './lib/types'
import { AdvisorView } from './views/AdvisorView'
import { AgentsView } from './views/AgentsView'
import { ManualView } from './views/ManualView'
import { NewsView } from './views/NewsView'
import { PnlView } from './views/PnlView'
import { ScannerView, type ChartRequest } from './views/ScannerView'
import { SwingView } from './views/SwingView'

function App() {
  const [tab, setTab] = useState<TabName>('scanner')
  const [stateData, setStateData] = useState<StateData | null>(null)
  const [mode, setMode] = useState<ModeState>({ mode: 'paper', live: false, executor: null })
  const [liveModalOpen, setLiveModalOpen] = useState(false)
  const [simBusy, setSimBusy] = useState(false)
  const [simStatus, setSimStatus] = useState<SimStatus | null>(null)
  const [simStatusText, setSimStatusText] = useState('')
  const [chartRequest, setChartRequest] = useState<ChartRequest | null>(null)
  const [symbols, setSymbols] = useState<string[]>([])
  const simPollRef = useRef<number | null>(null)

  useVisiblePolling(async () => {
    try {
      setStateData(await jget<StateData>('/api/state'))
    } catch {
      /* server briefly away — keep last view */
    }
  }, 10000)

  useVisiblePolling(async () => {
    try {
      setMode(await jget<ModeState>('/api/mode'))
    } catch {
      /* keep last */
    }
  }, 10000)

  async function onThresholdSet(v: number) {
    if (Number.isNaN(v)) return
    try {
      await jpost('/api/threshold', { value: v })
      setStateData(await jget<StateData>('/api/state'))
    } catch {
      /* best-effort */
    }
  }

  function simStatusMsg(st: SimStatus | null) {
    if (!st) return ''
    if (st.error) return 'sim error: ' + st.error
    if (st.running) return `simulating ${st.day || '…'}  ${st.done}/${st.total}  (${st.found} signals)`
    if ((st.total || 0) > 0) return `sim done: ${st.found} signals from ${st.day || 'last day'}`
    return ''
  }

  async function pollSim() {
    try {
      const st = await jget<SimStatus>('/api/simulate')
      setSimStatus(st)
      setSimStatusText(simStatusMsg(st))
      if (st.running) return
      if (simPollRef.current) {
        clearInterval(simPollRef.current)
        simPollRef.current = null
      }
      setSimBusy(false)
      setStateData(await jget<StateData>('/api/state'))
    } catch {
      if (simPollRef.current) {
        clearInterval(simPollRef.current)
        simPollRef.current = null
      }
      setSimBusy(false)
    }
  }

  useEffect(() => {
    ;(async () => {
      try {
        const st = await jget<SimStatus>('/api/simulate')
        if (st?.running) {
          setSimBusy(true)
          setSimStatusText(simStatusMsg(st))
          simPollRef.current = window.setInterval(pollSim, 3000)
        } else {
          setSimStatus(st)
        }
      } catch {
        /* dashboard is best-effort */
      }
    })()
    return () => {
      if (simPollRef.current) clearInterval(simPollRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function onSimulate() {
    if (simPollRef.current) return
    setSimBusy(true)
    setSimStatusText('starting…')
    try {
      await jpost('/api/simulate', {})
      simPollRef.current = window.setInterval(pollSim, 3000)
    } catch (e) {
      setSimStatusText('failed: ' + (e as Error).message)
      setSimBusy(false)
    }
  }

  function onSimRowClick(t: SimTrade) {
    setTab('scanner')
    setChartRequest({ symbol: t.symbol, direction: t.direction, bar_time: t.entry_time, nonce: Date.now() })
  }

  // symbol autocomplete for Strategy Advisor + Manual Evaluate inputs
  useEffect(() => {
    ;(async () => {
      try {
        const syms = await jget<string[]>('/api/symbols')
        if (Array.isArray(syms) && syms.length) setSymbols(syms)
      } catch {
        /* autocomplete is optional */
      }
    })()
  }, [])

  return (
    <div className="h-full flex flex-col overflow-hidden">
      <Header
        tab={tab}
        onTab={setTab}
        stateData={stateData}
        onThresholdSet={onThresholdSet}
        onOpenLive={() => setLiveModalOpen(true)}
        simBusy={simBusy}
        simStatus={simStatusText}
        onSimulate={onSimulate}
        mode={mode}
        onModeChange={setMode}
      />

      <LiveTradingModal open={liveModalOpen} onClose={() => setLiveModalOpen(false)} mode={mode} onModeChange={setMode} />

      {tab === 'scanner' ? <SimDash status={simStatus} onRowClick={onSimRowClick} /> : null}

      <ScannerView visible={tab === 'scanner'} stateData={stateData} chartRequest={chartRequest} />
      <SwingView visible={tab === 'swing'} />
      <AgentsView visible={tab === 'agents'} />
      <ManualView visible={tab === 'manual'} />
      <AdvisorView visible={tab === 'advisor'} />
      <NewsView visible={tab === 'news'} />
      <PnlView visible={tab === 'pnl'} />

      <AssistantWidget />

      <datalist id="symlist">
        {symbols.map((s) => (
          <option key={s} value={s} />
        ))}
      </datalist>
    </div>
  )
}

export default App

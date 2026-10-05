import { useState } from 'react'
import { ActivityView } from './components/Activity'
import { AgentsView } from './components/Agents'
import { ChainView } from './components/Chain'
import { Cockpit } from './components/Cockpit'
import { Header } from './components/Header'
import { Journal } from './components/Journal'
import { Reporting } from './components/Reporting'
import { Settings } from './components/Settings'
import { useLive } from './lib/live'

const TABS = ['Cockpit', 'Option Chain', 'Agents', 'Journal', 'Reporting', 'Settings', 'Activity'] as const
type Tab = typeof TABS[number]

export default function App() {
  const { snap, connected, equity } = useLive()
  const [tab, setTab] = useState<Tab>('Cockpit')

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <Header snap={snap} connected={connected} />
      <nav style={{ display: 'flex', gap: 8, padding: '10px 16px', borderBottom: '1px solid var(--line)', overflowX: 'auto' }}>
        {TABS.map((t) => (
          <div key={t} className={`tab ${t === tab ? 'active' : ''}`} onClick={() => setTab(t)}>{t}</div>
        ))}
      </nav>
      <main style={{ flex: 1, minHeight: 0, padding: 16, overflow: 'hidden' }}>
        {tab === 'Cockpit' && <Cockpit snap={snap} equity={equity} />}
        {tab === 'Option Chain' && <ChainView snap={snap} />}
        {tab === 'Agents' && <AgentsView snap={snap} />}
        {tab === 'Journal' && <Journal />}
        {tab === 'Reporting' && <Reporting />}
        {tab === 'Settings' && <Settings />}
        {tab === 'Activity' && <ActivityView snap={snap} />}
      </main>
    </div>
  )
}

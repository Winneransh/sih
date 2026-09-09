import { useEffect, useState } from 'react'
import ChatView from './components/ChatView'
import ModelsView from './components/ModelsView'
import KnowledgeView from './components/KnowledgeView'
import SystemView from './components/SystemView'
import StatusBar from './components/StatusBar'
import { api } from './api'

const VIEWS = [
  { id: 'chat', label: 'Workbench' },
  { id: 'models', label: 'Models' },
  { id: 'kb', label: 'Knowledge' },
  { id: 'system', label: 'System' },
]

export default function App() {
  const [view, setView] = useState('chat')
  const [health, setHealth] = useState(null)

  // The status bar polls; a stale reading is worse than no reading when
  // the whole point is showing what is running right now.
  useEffect(() => {
    let alive = true
    const tick = () => api.health().then(h => alive && setHealth(h)).catch(() => {})
    tick()
    const t = setInterval(tick, 5000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  return (
    <div className="h-full flex flex-col">
      <header className="h-11 shrink-0 flex items-center border-b border-line bg-panel px-3">
        <div className="flex items-center gap-2.5 pr-5 mr-2 border-r border-line">
          <Mark />
          <span className="text-sm font-medium tracking-tight">Workbench</span>
        </div>

        <nav className="flex gap-0.5">
          {VIEWS.map(v => (
            <button
              key={v.id}
              onClick={() => setView(v.id)}
              className={`px-3 py-1.5 rounded text-2xs font-medium transition-colors ${
                view === v.id
                  ? 'bg-raised text-ink'
                  : 'text-faint hover:text-dim'
              }`}
            >
              {v.label}
            </button>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <SovereignBadge net={health?.network} />
        </div>
      </header>

      <main className="flex-1 min-h-0">
        {view === 'chat' && <ChatView health={health} />}
        {view === 'models' && <ModelsView />}
        {view === 'kb' && <KnowledgeView />}
        {view === 'system' && <SystemView health={health} />}
      </main>

      <StatusBar health={health} />
    </div>
  )
}

function Mark() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden>
      <rect x="1" y="1" width="16" height="16" rx="2" className="stroke-line2" strokeWidth="1.2" />
      <path d="M5 9h2.2l1.3-3 1.6 6 1.2-3H13" className="stroke-live" strokeWidth="1.3"
            strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function SovereignBadge({ net }) {
  if (!net) return null
  const clean = net.external_calls === 0
  return (
    <div
      className="flex items-center gap-2 px-2.5 py-1 rounded border text-2xs font-mono"
      style={{
        borderColor: clean ? 'rgba(74,222,128,.3)' : 'rgba(248,113,113,.4)',
        color: clean ? '#4ADE80' : '#F87171',
      }}
      title="Outbound connections that are not loopback and not model provisioning"
    >
      <span className={`w-1.5 h-1.5 rounded-full ${clean ? 'bg-ok' : 'bg-fault'}`} />
      {net.external_calls} external
    </div>
  )
}

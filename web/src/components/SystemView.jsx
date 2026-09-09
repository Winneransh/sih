import { useEffect, useState } from 'react'
import { api } from '../api'

export default function SystemView({ health }) {
  const [net, setNet] = useState(null)
  const [agents, setAgents] = useState(null)
  const [events, setEvents] = useState([])

  useEffect(() => {
    let alive = true
    const tick = () => {
      api.network().then(d => alive && setNet(d)).catch(() => {})
      api.audit(120).then(d => alive && setEvents(d.events)).catch(() => {})
    }
    tick()
    api.agents().then(setAgents).catch(() => {})
    const t = setInterval(tick, 2000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  return (
    <div className="h-full overflow-y-auto">
      <div className="grid grid-cols-2 gap-3 p-3">
        <NetworkPanel net={net} />
        <AgentPanel agents={agents} health={health} />
      </div>
      <AuditPanel events={events} />
    </div>
  )
}

function NetworkPanel({ net }) {
  if (!net) return <Panel title="Network"><Loading /></Panel>

  if (!net.available) {
    return (
      <Panel title="Network">
        <p className="text-2xs text-warn">
          psutil is not installed, so outbound connections cannot be observed.
        </p>
      </Panel>
    )
  }

  const clean = net.external_calls === 0

  return (
    <Panel title="Network" action={
      <button className="btn" onClick={() => api.resetNetwork()}>Reset</button>
    }>
      <div className="flex items-baseline gap-3 mb-3">
        <span className={`text-4xl font-mono ${clean ? 'text-ok' : 'text-fault'}`}>
          {net.external_calls}
        </span>
        <span className="text-xs text-dim">
          external connections in {Math.round(net.uptime_s)}s
        </span>
      </div>

      <p className="text-2xs text-faint leading-relaxed mb-3">
        Counts outbound connections that are neither loopback nor model
        downloads. Model traffic runs entirely on 127.0.0.1, so this stays
        at zero while the system is working.
      </p>

      {net.provisioning_calls > 0 && (
        <p className="text-2xs text-dim mb-3">
          {net.provisioning_calls} provisioning connection
          {net.provisioning_calls === 1 ? '' : 's'} (model downloads), shown
          separately and not counted above.
        </p>
      )}

      {net.recent.length > 0 && (
        <div className="border-t border-line pt-2 space-y-0.5 max-h-40 overflow-y-auto">
          {net.recent.slice().reverse().map((r, i) => (
            <div key={i} className="flex gap-2 text-2xs font-mono">
              <span className={r.kind === 'external' ? 'text-fault' : 'text-faint'}>
                {r.kind}
              </span>
              <span className="text-dim truncate">{r.host}</span>
              <span className="ml-auto text-faint">{r.process}</span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  )
}

function AgentPanel({ agents, health }) {
  if (!agents) return <Panel title="Agents"><Loading /></Panel>

  return (
    <Panel title="Agents">
      <div className="space-y-1.5">
        {agents.available.map(a => (
          <div key={a.agent} className="flex items-start gap-2.5">
            <span className="w-1.5 h-1.5 rounded-full bg-ok mt-1.5 shrink-0" />
            <div className="min-w-0">
              <div className="flex items-baseline gap-2">
                <span className="text-xs font-mono">{a.agent}</span>
                <span className="text-2xs text-faint">
                  needs {a.capability_required}
                </span>
              </div>
              <p className="text-2xs text-faint leading-relaxed mt-0.5">
                {a.description}
              </p>
            </div>
          </div>
        ))}

        {agents.unavailable.map(a => (
          <div key={a.agent} className="flex items-center gap-2.5">
            <span className="w-1.5 h-1.5 rounded-full bg-line2 shrink-0" />
            <span className="text-xs font-mono text-faint">{a.agent}</span>
            <span className="text-2xs text-warn">
              no {a.missing_capability} model installed
            </span>
          </div>
        ))}
      </div>
    </Panel>
  )
}

function AuditPanel({ events }) {
  return (
    <div className="px-3 pb-3">
      <Panel title="Activity log">
        <div className="max-h-96 overflow-y-auto space-y-0.5">
          {events.length === 0 && <Loading />}
          {events.slice().reverse().map((e, i) => (
            <div key={i} className="flex gap-3 text-2xs font-mono py-0.5">
              <span className="text-faint shrink-0 w-20">
                {e.ts?.slice(11, 23)}
              </span>
              <span className={`shrink-0 w-44 truncate ${eventTone(e.event)}`}>
                {e.event}
              </span>
              <span className="text-faint truncate">{detail(e)}</span>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  )
}

function eventTone(ev = '') {
  if (ev.includes('error') || ev.includes('failed')) return 'text-fault'
  if (ev.startsWith('netmon.external')) return 'text-fault'
  if (ev.startsWith('model.')) return 'text-live'
  if (ev.startsWith('planner.')) return 'text-warn'
  return 'text-dim'
}

function detail(e) {
  const skip = new Set(['ts', 'event'])
  return Object.entries(e)
    .filter(([k]) => !skip.has(k))
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join('  ')
    .slice(0, 220)
}

function Panel({ title, action, children }) {
  return (
    <section className="border border-line rounded bg-panel">
      <header className="px-3 py-2 border-b border-line flex items-center">
        <h2 className="text-2xs font-medium text-dim">{title}</h2>
        {action && <div className="ml-auto">{action}</div>}
      </header>
      <div className="p-3">{children}</div>
    </section>
  )
}

function Loading() {
  return <p className="text-2xs text-faint">Loading…</p>
}

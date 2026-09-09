import { useMemo } from 'react'

// The plan arrives before any work starts, so the chain is visible first and
// then fills in. That ordering is the point: you see what the system intends,
// then watch it happen, rather than watching decisions dribble out.

export default function PlanTrace({ plan, events, composing }) {
  const state = useMemo(() => buildState(plan, events), [plan, events])

  if (!plan) {
    return (
      <div className="text-2xs text-faint font-mono flex items-center gap-2">
        <Pulse /> planning
      </div>
    )
  }

  // The planner decided no work was needed — this is conversation, not a task.
  if (!plan.steps?.length) {
    return (
      <div className="text-2xs text-faint font-mono flex items-center gap-2">
        <Pulse /> answering
      </div>
    )
  }

  return (
    <div className="border border-line rounded bg-panel overflow-hidden">
      <div className="px-3 py-2 border-b border-line flex items-center gap-3">
        <span className="text-2xs text-faint font-mono">plan</span>
        <span className="text-xs font-mono text-dim">{state.chain}</span>
        {plan.deliverable && (
          <span className="ml-auto tag">{plan.deliverable}</span>
        )}
      </div>

      {plan.reasoning && (
        <p className="px-3 py-2 text-2xs text-faint border-b border-line leading-relaxed">
          {plan.reasoning}
        </p>
      )}

      <ol className="divide-y divide-line">
        {state.steps.map(s => <Step key={s.id} s={s} />)}
      </ol>

      {composing && (
        <div className="px-3 py-2 border-t border-line flex items-center gap-2.5">
          <Pulse />
          <span className="text-2xs font-mono text-live">composing answer</span>
        </div>
      )}

      {plan.validation?.length > 0 && (
        <div className="px-3 py-2 border-t border-line space-y-0.5">
          {plan.validation.map((v, i) => (
            <div key={i} className="text-2xs text-warn/80 font-mono">{v}</div>
          ))}
        </div>
      )}
    </div>
  )
}

function Step({ s }) {
  const tone = {
    pending: 'text-faint',
    running: 'text-live',
    done: 'text-ink',
    failed: 'text-fault',
  }[s.status]

  return (
    <li className="px-3 py-2">
      <div className="flex items-center gap-2.5">
        <Indicator status={s.status} />
        <span className={`text-xs font-mono ${tone}`}>{s.agent}</span>

        <span className="tag">{s.cardinality}</span>
        {s.total > 1 && (
          <span className="text-2xs font-mono text-faint">
            {s.done}/{s.total} {s.execution === 'parallel' ? `∥${s.maxParallel}` : 'seq'}
          </span>
        )}

        {s.model && (
          <span className="ml-auto text-2xs font-mono text-faint truncate max-w-[45%]"
                title={s.model}>
            {shortModel(s.model)}
          </span>
        )}
      </div>

      {s.task && (
        <p className="text-2xs text-faint mt-1 ml-5 leading-relaxed line-clamp-2">
          {s.task}
        </p>
      )}

      {s.summaries?.length > 0 && (
        <div className="ml-5 mt-1.5 space-y-0.5">
          {s.summaries.slice(0, 3).map((x, i) => (
            <div key={i} className="text-2xs text-dim leading-relaxed">{x}</div>
          ))}
        </div>
      )}

      {s.total > 1 && s.status === 'running' && (
        <div className="ml-5 mt-1.5 h-0.5 bg-line rounded overflow-hidden">
          <div className="h-full bg-live transition-all duration-300"
               style={{ width: `${(s.done / s.total) * 100}%` }} />
        </div>
      )}
    </li>
  )
}

function Indicator({ status }) {
  if (status === 'running') return <Pulse />
  const cls = {
    pending: 'border-line2',
    done: 'border-ok bg-ok/20',
    failed: 'border-fault bg-fault/20',
  }[status]
  return <span className={`w-2 h-2 rounded-full border ${cls} shrink-0`} />
}

function Pulse() {
  return (
    <span className="relative w-2 h-2 shrink-0">
      <span className="absolute inset-0 rounded-full bg-live animate-ping opacity-60" />
      <span className="absolute inset-0 rounded-full bg-live" />
    </span>
  )
}

function shortModel(m) {
  const t = m.split('/').pop() || m
  return t.replace(/-GGUF$/i, '')
}

function buildState(plan, events) {
  if (!plan) return { chain: '', steps: [] }

  const steps = (plan.steps || []).map(s => ({
    id: s.id,
    agent: s.agent,
    model: s.model,
    task: s.task,
    cardinality: s.cardinality,
    execution: s.execution,
    maxParallel: s.max_parallel,
    status: 'pending',
    done: 0,
    total: 1,
    summaries: [],
  }))
  const byId = Object.fromEntries(steps.map(s => [s.id, s]))

  for (const e of events) {
    const s = byId[e.step]
    if (!s) continue
    if (e.type === 'step.route') s.status = 'running'
    else if (e.type === 'step.start') {
      s.status = 'running'
      s.total = e.n_runs ?? 1
    } else if (e.type === 'step.progress') {
      s.done = e.done
      s.total = e.total ?? s.total
    } else if (e.type === 'step.end') {
      s.status = e.ok ? 'done' : 'failed'
      s.done = s.total
      s.summaries = e.summaries || []
    }
  }

  const chain = steps.map(s =>
    s.total > 1 ? `${s.agent}×${s.total}` : s.agent).join(' → ')

  return { chain, steps }
}

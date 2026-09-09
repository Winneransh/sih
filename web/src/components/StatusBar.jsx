export default function StatusBar({ health }) {
  if (!health) {
    return (
      <footer className="h-6 shrink-0 border-t border-line bg-panel
                         flex items-center px-3 text-2xs font-mono text-faint">
        connecting to backend…
      </footer>
    )
  }

  const running = health.models_running || []
  const ramPct = health.ram_total_gb
    ? Math.round((health.ram_used_gb / health.ram_total_gb) * 100)
    : null

  return (
    <footer className="h-6 shrink-0 border-t border-line bg-panel
                       flex items-center gap-4 px-3 text-2xs font-mono text-faint">
      <span>{health.platform} {health.machine}</span>
      <span>{health.backend}</span>

      <span className="flex items-center gap-1.5">
        {running.length > 0
          ? running.map(r => (
              <span key={r.model} className="flex items-center gap-1"
                    title={r.model}>
                <span className={`w-1 h-1 rounded-full ${
                  r.healthy ? 'bg-ok' : 'bg-fault'}`} />
                :{r.port}
              </span>
            ))
          : <span>no models loaded</span>}
      </span>

      {ramPct !== null && (
        <span className={ramPct > 85 ? 'text-warn' : ''}>
          ram {health.ram_used_gb}/{health.ram_total_gb} GB
        </span>
      )}

      <span className="ml-auto">
        kb {health.kb?.n_chunks ?? 0} chunks
      </span>
      <span>{health.agents?.length ?? 0} agents</span>
    </footer>
  )
}

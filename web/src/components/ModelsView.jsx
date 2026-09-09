import { useEffect, useState } from 'react'
import { api } from '../api'
import Markdown from './Markdown'

export default function ModelsView() {
  const [tab, setTab] = useState('installed')
  const [installed, setInstalled] = useState({ models: {}, running: [] })
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [selected, setSelected] = useState(null)
  const [info, setInfo] = useState(null)
  const [loading, setLoading] = useState(false)
  const [pulling, setPulling] = useState(null)

  const refresh = () => api.installed().then(setInstalled).catch(() => {})

  useEffect(() => { refresh() }, [])

  const search = async (q) => {
    setLoading(true); setSelected(null); setInfo(null)
    try { setResults((await api.browse(q, 40)).models) }
    finally { setLoading(false) }
  }

  useEffect(() => { if (tab === 'browse' && results.length === 0) search('') }, [tab])

  const open = async (repo) => {
    setSelected(repo); setInfo(null)
    try { setInfo(await api.modelInfo(repo)) }
    catch (e) { setInfo({ error: String(e) }) }
  }

  const download = async (filename) => {
    setPulling(filename)
    try {
      await api.pull({ repo_id: selected, filename })
      await refresh()
      setTab('installed')
    } catch (e) {
      alert(`Download failed: ${e}`)
    } finally {
      setPulling(null)
    }
  }

  const runningPorts = Object.fromEntries(
    installed.running.map(r => [r.model, r]))

  return (
    <div className="h-full flex flex-col">
      <div className="h-10 shrink-0 flex items-center gap-1 px-3 border-b border-line bg-panel">
        {['installed', 'browse'].map(t => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-3 py-1 rounded text-2xs font-medium transition-colors ${
              tab === t ? 'bg-raised text-ink' : 'text-faint hover:text-dim'}`}>
            {t === 'installed'
              ? `Installed (${Object.keys(installed.models).length})`
              : 'Browse Hugging Face'}
          </button>
        ))}
        {tab === 'installed' && installed.running.length > 0 && (
          <button className="btn ml-auto" onClick={() => api.unloadAll().then(refresh)}>
            Unload all
          </button>
        )}
      </div>

      {tab === 'installed' ? (
        <InstalledList models={installed.models} running={runningPorts}
                       onRefresh={refresh} />
      ) : (
        <div className="flex-1 min-h-0 flex">
          <div className="w-96 shrink-0 border-r border-line flex flex-col">
            <div className="p-2 border-b border-line">
              <input
                className="field"
                placeholder="Search models, or leave empty for trending"
                value={query}
                onChange={e => setQuery(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && search(query)}
              />
            </div>
            <div className="flex-1 overflow-y-auto">
              {loading && <p className="p-3 text-2xs text-faint">Searching…</p>}
              {results.map(m => (
                <button key={m.repo_id} onClick={() => open(m.repo_id)}
                  className={`w-full text-left px-3 py-2 border-b border-line/60
                              transition-colors ${
                    selected === m.repo_id ? 'bg-raised' : 'hover:bg-raised/50'}`}>
                  <div className="text-xs font-mono truncate">{m.repo_id}</div>
                  <div className="text-2xs text-faint mt-0.5">
                    {fmt(m.downloads)} downloads · {m.likes} likes
                  </div>
                </button>
              ))}
            </div>
          </div>

          <div className="flex-1 min-w-0 overflow-y-auto">
            {!selected && (
              <p className="p-6 text-sm text-faint max-w-md leading-relaxed">
                Pick a model to see its card and available quantizations. Files are
                checked against this machine's memory before you download.
              </p>
            )}
            {selected && !info && <p className="p-6 text-2xs text-faint">Loading…</p>}
            {info && <ModelDetail info={info} onDownload={download}
                                  pulling={pulling} />}
          </div>
        </div>
      )}
    </div>
  )
}

function InstalledList({ models, running, onRefresh }) {
  const entries = Object.entries(models)
  if (entries.length === 0) {
    return (
      <p className="p-6 text-sm text-faint max-w-md leading-relaxed">
        Nothing installed yet. Open <b className="text-dim">Browse Hugging Face</b> and
        download a model. You need at least one text model; add a vision model for
        scanned documents and an embedding model for the knowledge base.
      </p>
    )
  }
  return (
    <div className="flex-1 overflow-y-auto p-3 space-y-2">
      {entries.map(([repo, m]) => {
        const run = running[repo]
        return (
          <div key={repo}
               className="border border-line rounded bg-panel px-3 py-2.5">
            <div className="flex items-center gap-3">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                run?.healthy ? 'bg-ok' : 'bg-line2'}`} />
              <span className="text-xs font-mono truncate">{repo}</span>
              <div className="ml-auto flex gap-1.5 shrink-0">
                {run ? (
                  <button className="btn" onClick={() => api.unload(repo).then(onRefresh)}>
                    Unload
                  </button>
                ) : (
                  <button className="btn btn-primary"
                          onClick={() => api.load(repo).then(onRefresh).catch(e => alert(e))}>
                    Load
                  </button>
                )}
                <button className="btn btn-danger" onClick={() => {
                  if (confirm(`Delete ${repo} and its files?`))
                    api.eject(repo).then(onRefresh)
                }}>
                  Eject
                </button>
              </div>
            </div>

            <div className="flex flex-wrap gap-1.5 mt-2 ml-4">
              {(m.capabilities || []).map(c => (
                <span key={c} className={`tag ${
                  c === 'vision' ? 'text-live border-live/30' :
                  c === 'coding' ? 'text-warn border-warn/30' : ''}`}>
                  {c}
                </span>
              ))}
              <span className="tag">{gb(m.size_bytes)} GB</span>
              {m.license && <span className="tag">{m.license}</span>}
              {run && <span className="tag text-ok border-ok/30">:{run.port}</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function ModelDetail({ info, onDownload, pulling }) {
  if (info.error) return <p className="p-6 text-xs text-fault">{info.error}</p>

  const weights = info.files.filter(f => !f.is_mmproj)
  const mmproj = info.files.filter(f => f.is_mmproj)
  const busy = !!pulling

  return (
    <div>
      {/* Header — sticky so the repo name stays visible while reading the card */}
      <div className="sticky top-0 z-10 bg-base border-b border-line px-5 py-4">
        <h2 className="text-sm font-mono text-ink mb-1.5 break-all">
          {info.repo_id}
        </h2>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-2xs text-faint">
          <span>{fmt(info.downloads)} downloads</span>
          <span>{info.likes} likes</span>
          <span>fits under {info.ram_budget_gb} GB</span>
          {mmproj.length > 0 && (
            <span className="tag text-live border-live/30">vision</span>
          )}
          {info.tags?.slice(0, 3).map(t => (
            <span key={t} className="tag">{t}</span>
          ))}
        </div>
      </div>

      <div className="px-5 py-4">
        {mmproj.length > 0 && (
          <p className="mb-4 text-2xs text-live">
            Vision model — the projector file downloads automatically with the
            weights.
          </p>
        )}

        <h3 className="text-2xs font-medium text-dim mb-2">
          Choose a quantization
        </h3>
        <div className="border border-line rounded overflow-hidden mb-6">
          {weights.map((f, i) => {
            const recommended = f.filename === info.recommended
            const thisOne = pulling === f.filename
            return (
              <div key={f.filename}
                   className={`flex items-center gap-3 px-3 py-2.5 ${
                     i > 0 ? 'border-t border-line' : ''
                   } ${recommended ? 'bg-live/5' : ''}`}>
                <FitDot fit={f.fit} />

                <div className="min-w-0 flex-1">
                  <div className="text-xs font-mono text-ink truncate">
                    {quantLabel(f.filename)}
                  </div>
                  <div className="text-2xs text-faint truncate">
                    {f.filename}
                  </div>
                </div>

                {recommended && (
                  <span className="tag text-live border-live/30 shrink-0">
                    recommended
                  </span>
                )}
                <span className="text-2xs font-mono text-dim shrink-0 w-16 text-right">
                  {f.size_gb} GB
                </span>

                <button
                  className={`btn shrink-0 w-24 text-center ${
                    recommended ? 'btn-primary' : ''}`}
                  disabled={busy || f.fit === 'too_large'}
                  onClick={() => onDownload(f.filename)}
                >
                  {thisOne ? 'Downloading' :
                   f.fit === 'too_large' ? 'Too large' : 'Download'}
                </button>
              </div>
            )
          })}
        </div>

        {info.card && (
          <>
            <h3 className="text-2xs font-medium text-dim mb-1">Model card</h3>
            <div className="border-t border-line pt-3">
              <Markdown text={info.card} />
            </div>
          </>
        )}
      </div>
    </div>
  )
}

// Unsloth and others ship UD-Q4_K_XL, IQ4_NL and similar. Pull the readable
// part out for the primary label and keep the full filename underneath.
function quantLabel(filename) {
  const m = filename.match(/(UD-)?(I?Q\d+[_A-Z0-9]*|BF16|F16|F32)/i)
  return m ? m[0].toUpperCase() : filename.replace(/\.gguf$/i, '')
}

function FitDot({ fit }) {
  const c = { good: 'bg-ok', tight: 'bg-warn', too_large: 'bg-fault' }[fit]
  const t = { good: 'Fits comfortably in memory',
              tight: 'Tight fit — little headroom left',
              too_large: 'Will not fit in available memory' }[fit]
  return <span className={`w-2 h-2 rounded-full shrink-0 ${c}`} title={t} />
}

const fmt = n => (n || 0).toLocaleString()
const gb = b => ((b || 0) / 1024 ** 3).toFixed(1)

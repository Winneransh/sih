import { useEffect, useState } from 'react'
import { api } from '../api'

export default function KnowledgeView() {
  const [stats, setStats] = useState(null)
  const [q, setQ] = useState('')
  const [hits, setHits] = useState([])
  const [busy, setBusy] = useState(false)

  const refresh = () => api.kbStats().then(setStats).catch(() => {})
  useEffect(() => { refresh() }, [])

  const ingest = async (fileList) => {
    setBusy(true)
    try {
      for (const f of Array.from(fileList)) {
        await api.kbUpload(f)
      }
      refresh()
    } catch (e) {
      alert(`Ingest failed: ${e}`)
    } finally { setBusy(false) }
  }

  const search = async () => {
    if (!q.trim()) return
    setBusy(true)
    try { setHits((await api.kbSearch(q)).hits) }
    catch (e) { alert(String(e)) }
    finally { setBusy(false) }
  }

  return (
    <div className="h-full flex">
      <aside className="w-64 shrink-0 border-r border-line bg-panel p-3 space-y-4">
        <div>
          <h2 className="text-2xs text-dim mb-2">Indexed</h2>
          <div className="text-2xl font-mono">{stats?.n_chunks ?? '—'}</div>
          <div className="text-2xs text-faint">
            chunks from {stats?.n_docs ?? 0} document{stats?.n_docs === 1 ? '' : 's'}
          </div>
        </div>

        <label className="btn btn-primary w-full text-center cursor-pointer block">
          {busy ? 'Working…' : 'Add documents'}
          <input type="file" multiple hidden disabled={busy}
                 onChange={e => ingest(e.target.files)} />
        </label>

        <p className="text-2xs text-faint leading-relaxed">
          Manuals, procedures and past correspondence. Text PDFs are chunked
          and embedded on upload. Name a document in chat and it is searched
          directly rather than looked at as an image.
        </p>

        {stats?.docs?.length > 0 && (
          <div>
            <h3 className="text-2xs text-dim mb-1.5">Documents</h3>
            <div className="space-y-0.5">
              {stats.docs.map(d => (
                <div key={d} className="text-2xs font-mono text-faint truncate" title={d}>
                  {d}
                </div>
              ))}
            </div>
          </div>
        )}

        {stats?.n_chunks > 0 && (
          <button className="btn btn-danger w-full" onClick={() => {
            if (confirm('Clear the whole index?')) api.kbClear().then(refresh)
          }}>
            Clear index
          </button>
        )}
      </aside>

      <section className="flex-1 min-w-0 flex flex-col">
        <div className="p-3 border-b border-line flex gap-2">
          <input className="field" placeholder="Search the knowledge base"
                 value={q} onChange={e => setQ(e.target.value)}
                 onKeyDown={e => e.key === 'Enter' && search()} />
          <button className="btn shrink-0" onClick={search} disabled={busy}>Search</button>
        </div>

        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {hits.length === 0 && (
            <p className="text-sm text-faint max-w-md leading-relaxed p-2">
              Search returns the passages an agent would ground its answer in, with
              the document and page it came from.
            </p>
          )}
          {hits.map((h, i) => (
            <div key={i} className="border border-line rounded bg-panel p-3">
              <div className="flex items-center gap-2 mb-1.5">
                <span className="tag">{h.doc}{h.page ? `, p.${h.page}` : ''}</span>
                {h.section && (
                  <span className="text-2xs text-faint truncate">{h.section}</span>
                )}
                <span className="ml-auto text-2xs font-mono text-faint">
                  {Number(h.score).toFixed(3)}
                </span>
              </div>
              <p className="text-xs text-dim leading-relaxed">{h.text}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}

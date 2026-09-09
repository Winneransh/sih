import { useEffect, useRef, useState } from 'react'
import { api, chatStream } from '../api'
import PlanTrace from './PlanTrace'

export default function ChatView({ health }) {
  const [conversations, setConversations] = useState([])
  const [cid, setCid] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [files, setFiles] = useState([])
  const [busy, setBusy] = useState(false)
  const [events, setEvents] = useState([])
  const [plan, setPlan] = useState(null)
  const [deliverables, setDeliverables] = useState([])
  const [composing, setComposing] = useState(false)
  const bottom = useRef(null)

  const loadConversations = () =>
    api.sessions().then(d => setConversations(d.conversations)).catch(() => {})

  useEffect(() => { loadConversations() }, [])

  useEffect(() => {
    if (!cid) { setMessages([]); return }
    api.session(cid).then(d => setMessages(d.messages)).catch(() => {})
    api.deliverables(cid).then(d => setDeliverables(d.files)).catch(() => {})
  }, [cid])

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, events])

  const attach = async (fileList) => {
    const sid = cid || (await api.newSession()).conversation_id
    if (!cid) { setCid(sid); loadConversations() }
    for (const f of Array.from(fileList)) {
      try {
        const rec = await api.upload(sid, f)
        setFiles(prev => [...prev, rec])
      } catch (e) {
        setFiles(prev => [...prev, { name: f.name, kind: 'error', error: String(e) }])
      }
    }
  }

  const send = async () => {
    if (!input.trim() || busy) return
    const text = input.trim()
    setInput('')
    setBusy(true)
    setEvents([])
    setPlan(null)
    setComposing(false)
    setMessages(prev => [...prev, { role: 'user', content: text, meta: {} }])

    let conversationId = cid
    try {
      await chatStream(
        { message: text, conversation_id: cid, session_id: cid },
        (ev) => {
          if (ev.type === 'plan') setPlan(ev.plan)
          else if (ev.type === 'composing') setComposing(true)
          else if (ev.type === 'done') {
            conversationId = ev.conversation_id
            setMessages(prev => [...prev, {
              role: 'assistant', content: ev.answer,
              meta: { deliverables: ev.deliverables, sources: ev.sources, ok: ev.ok },
            }])
            setDeliverables(ev.deliverables.map(p => ({ name: p.split('/').pop(), path: p })))
            setFiles([])
            setComposing(false)
          } else if (ev.type === 'error') {
            setMessages(prev => [...prev, {
              role: 'assistant', content: `Run failed: ${ev.error}`, meta: { ok: false },
            }])
          } else {
            setEvents(prev => [...prev, ev])
          }
        }
      )
      if (!cid && conversationId) { setCid(conversationId); loadConversations() }
    } finally {
      setBusy(false)
    }
  }

  const noModels = health && health.models_installed === 0

  return (
    <div className="h-full flex">
      {/* Conversations */}
      <aside className="w-52 shrink-0 border-r border-line bg-panel flex flex-col">
        <div className="p-2">
          <button
            className="btn w-full"
            onClick={async () => {
              const d = await api.newSession()
              setCid(d.conversation_id); setMessages([]); setEvents([])
              setPlan(null); setFiles([]); loadConversations()
            }}
          >
            New chat
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5">
          {conversations.map(c => (
            <button
              key={c.id}
              onClick={() => setCid(c.id)}
              className={`w-full text-left px-2 py-1.5 rounded text-xs truncate transition-colors ${
                cid === c.id ? 'bg-raised text-ink' : 'text-faint hover:text-dim'
              }`}
            >
              {c.title || 'Untitled'}
            </button>
          ))}
          {conversations.length === 0 && (
            <p className="text-2xs text-faint px-2 py-3 leading-relaxed">
              No chats yet. Attach a document or ask a question to start.
            </p>
          )}
        </div>
      </aside>

      {/* Thread */}
      <section className="flex-1 min-w-0 flex flex-col">
        <div
          className="flex-1 overflow-y-auto px-6 py-5"
          onDragOver={e => e.preventDefault()}
          onDrop={e => { e.preventDefault(); attach(e.dataTransfer.files) }}
        >
          {noModels && (
            <Notice>
              No models installed. Open <b className="text-ink">Models</b> and download one
              to get started.
            </Notice>
          )}

          {messages.length === 0 && !noModels && <EmptyState />}

          <div className="max-w-3xl mx-auto space-y-5">
            {messages.map((m, i) => <Message key={i} m={m} />)}
            {busy && <PlanTrace plan={plan} events={events} composing={composing} />}
            <div ref={bottom} />
          </div>
        </div>

        {/* Composer */}
        <div className="border-t border-line bg-panel px-6 py-3">
          <div className="max-w-3xl mx-auto">
            {files.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mb-2">
                {files.map((f, i) => (
                  <span key={i} className="tag flex items-center gap-1.5">
                    {f.name}
                    <span className={f.kind === 'error' ? 'text-fault' : 'text-faint'}>
                      {f.kind}
                    </span>
                  </span>
                ))}
              </div>
            )}

            <div className="flex gap-2 items-end">
              <label className="btn cursor-pointer shrink-0" title="Attach files">
                Attach
                <input type="file" multiple hidden
                       onChange={e => attach(e.target.files)} />
              </label>

              <textarea
                rows={1}
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
                }}
                placeholder="Ask a question, or describe the document you need"
                className="field resize-none max-h-40"
                style={{ minHeight: 38 }}
                disabled={busy}
              />

              <button className="btn btn-primary shrink-0" onClick={send}
                      disabled={busy || !input.trim()}>
                {busy ? 'Working' : 'Run'}
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* Deliverables */}
      <aside className="w-60 shrink-0 border-l border-line bg-panel flex flex-col">
        <h2 className="px-3 py-2.5 text-2xs font-medium text-dim border-b border-line">
          Deliverables
        </h2>
        <div className="flex-1 overflow-y-auto p-2 space-y-1">
          {deliverables.length === 0 && (
            <p className="text-2xs text-faint px-1 py-2 leading-relaxed">
              Generated documents, decks, spreadsheets and code appear here.
            </p>
          )}
          {deliverables.map((f, i) => (
            <a key={i} href={api.downloadUrl(f.path)} download
               className="block px-2 py-1.5 rounded border border-line bg-raised
                          hover:border-line2 transition-colors">
              <div className="text-xs truncate">{f.name}</div>
              <div className="text-2xs text-faint font-mono">
                {f.name.split('.').pop()}
              </div>
            </a>
          ))}
        </div>
      </aside>
    </div>
  )
}

function Message({ m }) {
  const user = m.role === 'user'
  return (
    <div className={user ? 'flex justify-end' : ''}>
      <div className={user
        ? 'max-w-[80%] bg-raised border border-line rounded px-3 py-2 text-sm whitespace-pre-wrap'
        : 'max-w-full'}>
        {!user && (
          <div className="text-2xs text-faint mb-1.5 font-mono">assistant</div>
        )}
        <div className="text-sm whitespace-pre-wrap leading-relaxed">{m.content}</div>

        {m.meta?.sources?.length > 0 && (
          <div className="mt-2.5 flex flex-wrap gap-1">
            {dedupeSources(m.meta.sources).slice(0, 8).map((s, i) => (
              <span key={i} className="tag">
                {s.doc}{s.page ? `, p.${s.page}` : ''}
              </span>
            ))}
          </div>
        )}

        {m.meta?.deliverables?.length > 0 && (
          <div className="mt-2.5 space-y-1">
            {m.meta.deliverables.map((p, i) => (
              <a key={i} href={api.downloadUrl(p)} download
                 className="inline-block text-2xs font-mono text-live hover:underline mr-3">
                {p.split('/').pop()}
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function dedupeSources(list) {
  const seen = new Set()
  return list.filter(s => {
    const k = `${s.doc}|${s.page}`
    if (seen.has(k)) return false
    seen.add(k); return true
  })
}

function Notice({ children }) {
  return (
    <div className="max-w-3xl mx-auto mb-5 px-3 py-2.5 rounded border border-warn/30
                    bg-warn/5 text-warn text-xs">
      {children}
    </div>
  )
}

function EmptyState() {
  const examples = [
    'Read the attached inspection report and draft an approval note',
    'Extract the tag list and title block from this P&ID',
    'Write a script that parses these logs and summarise what it found',
    'What does our SOP say about quarterly valve inspection?',
  ]
  return (
    <div className="max-w-3xl mx-auto pt-16">
      <h2 className="text-base font-medium mb-1">Nothing leaves this machine.</h2>
      <p className="text-dim text-sm mb-6 max-w-lg leading-relaxed">
        Attach scanned reports, drawings or spreadsheets. The planner picks which
        agents run and which local model serves each one.
      </p>
      <div className="space-y-1.5">
        {examples.map((e, i) => (
          <div key={i}
               className="text-xs text-faint border-l border-line pl-3 py-0.5">
            {e}
          </div>
        ))}
      </div>
    </div>
  )
}

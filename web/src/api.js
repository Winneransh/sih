// Thin wrapper over the backend.
//
// In the browser, Vite proxies /api to port 8000. In the packaged app the
// shell picks a free port at launch and tells us which one, so requests are
// rewritten to an absolute URL. Everything below is written against /api
// either way.

let BASE = ''

export async function initBase() {
  // The Tauri injection script is meant to run before onload, but on some
  // platforms it lands late — so poll briefly instead of assuming it is
  // already there. In the browser this loop just times out and we fall back
  // to the Vite proxy, which costs nothing.
  const findInvoke = () => window.__TAURI__?.core?.invoke
  let invoke = findInvoke()
  for (let i = 0; i < 20 && !invoke; i++) {
    await new Promise(r => setTimeout(r, 50))
    invoke = findInvoke()
  }
  if (!invoke) return ''            // browser: Vite proxy handles /api

  try {
    const port = await invoke('backend_port')
    BASE = `http://127.0.0.1:${port}`
  } catch {
    BASE = 'http://127.0.0.1:8000'
  }
  return BASE
}

const url = (path) => BASE + path

const j = async (r) => {
  if (!r.ok) throw new Error((await r.text()) || r.statusText)
  return r.json()
}

export const api = {
  // models
  browse: (q = '', limit = 40) =>
    fetch(url(`/api/models/browse?q=${encodeURIComponent(q)}&limit=${limit}`)).then(j),
  modelInfo: (repo) => fetch(url(`/api/models/info/${repo}`)).then(j),
  pull: (body) =>
    fetch(url('/api/models/pull'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(j),
  installed: () => fetch(url('/api/models/installed')).then(j),
  load: (repo) => fetch(url(`/api/models/load/${repo}`), { method: 'POST' }).then(j),
  unload: (repo) => fetch(url(`/api/models/unload/${repo}`), { method: 'POST' }).then(j),
  unloadAll: () => fetch(url('/api/models/unload_all'), { method: 'POST' }).then(j),
  eject: (repo) => fetch(url(`/api/models/eject/${repo}`), { method: 'DELETE' }).then(j),

  // sessions
  sessions: () => fetch(url('/api/sessions')).then(j),
  session: (id) => fetch(url(`/api/sessions/${id}`)).then(j),
  newSession: () => fetch(url('/api/sessions'), { method: 'POST' }).then(j),
  delSession: (id) => fetch(url(`/api/sessions/${id}`), { method: 'DELETE' }).then(j),

  // files
  upload: (sessionId, file) => {
    const fd = new FormData()
    fd.append('session_id', sessionId)
    fd.append('file', file)
    return fetch(url('/api/files/upload'), { method: 'POST', body: fd }).then(j)
  },
  downloadUrl: (path) => url(`/api/files/download?path=${encodeURIComponent(path)}`),
  deliverables: (sid) => fetch(url(`/api/files/deliverables/${sid}`)).then(j),

  // knowledge base
  kbStats: () => fetch(url('/api/kb/stats')).then(j),
  kbSearch: (q) => fetch(url(`/api/kb/search?q=${encodeURIComponent(q)}`)).then(j),
  kbUpload: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return fetch(url('/api/kb/ingest_upload'), { method: 'POST', body: fd }).then(j)
  },
  kbClear: () => fetch(url('/api/kb'), { method: 'DELETE' }).then(j),

  // system
  health: () => fetch(url('/api/system/health')).then(j),
  network: () => fetch(url('/api/system/network')).then(j),
  resetNetwork: () => fetch(url('/api/system/network/reset'), { method: 'POST' }).then(j),
  audit: (n = 150) => fetch(url(`/api/system/audit?n=${n}`)).then(j),
  agents: () => fetch(url('/api/system/agents')).then(j),
}

// Streams plan and step events from the backend as they happen.
export async function chatStream(body, onEvent) {
  const res = await fetch(url('/api/chat/stream'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const parts = buf.split('\n\n')
    buf = parts.pop()
    for (const p of parts) {
      const line = p.trim()
      if (!line.startsWith('data: ')) continue
      try { onEvent(JSON.parse(line.slice(6))) } catch { /* partial frame */ }
    }
  }
}

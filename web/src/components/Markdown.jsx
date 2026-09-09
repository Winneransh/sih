// Minimal markdown renderer for model cards.
//
// Hugging Face cards are the main thing a person reads before downloading a
// model, and showing them as a raw <pre> block makes a good card look like a
// config dump. This handles what cards actually contain — headings, lists,
// tables, code, links, images — and deliberately does not try to be a
// complete implementation.

function inline(text, key) {
  const nodes = []
  let rest = text
  let i = 0

  // Order matters: code first so its contents are not re-parsed.
  const patterns = [
    [/`([^`]+)`/, (m) => <code key={`c${key}${i++}`}
        className="px-1 py-0.5 rounded bg-raised text-live font-mono text-[0.9em]">{m[1]}</code>],
    [/\*\*([^*]+)\*\*/, (m) => <strong key={`b${key}${i++}`} className="text-ink">{m[1]}</strong>],
    [/\[([^\]]+)\]\(([^)]+)\)/, (m) => <span key={`l${key}${i++}`} className="text-live">{m[1]}</span>],
    [/\*([^*]+)\*/, (m) => <em key={`i${key}${i++}`}>{m[1]}</em>],
  ]

  outer: while (rest) {
    let best = null
    for (const [re, render] of patterns) {
      const m = rest.match(re)
      if (m && (best === null || m.index < best.m.index)) best = { m, render }
    }
    if (!best) { nodes.push(rest); break outer }
    if (best.m.index > 0) nodes.push(rest.slice(0, best.m.index))
    nodes.push(best.render(best.m))
    rest = rest.slice(best.m.index + best.m[0].length)
  }
  return nodes
}

export default function Markdown({ text, className = '' }) {
  if (!text) return null

  // Strip the YAML frontmatter — it is metadata we already show as chips.
  let src = text.replace(/^---\n[\s\S]*?\n---\n/, '')
  // Badge and logo images add nothing at this size.
  src = src.replace(/!\[[^\]]*\]\([^)]*\)/g, '')
  src = src.replace(/<img[^>]*>/gi, '')
  src = src.replace(/<\/?(div|p|br|a|em|strong)[^>]*>/gi, '')

  const lines = src.split('\n')
  const out = []
  let i = 0
  let key = 0

  while (i < lines.length) {
    const line = lines[i]

    // fenced code
    if (line.trim().startsWith('```')) {
      const lang = line.trim().slice(3)
      const buf = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        buf.push(lines[i]); i++
      }
      i++
      out.push(
        <pre key={key++} className="my-2 p-2.5 rounded bg-base border border-line
                                    overflow-x-auto text-2xs font-mono text-dim">
          {buf.join('\n')}
        </pre>)
      continue
    }

    // table
    if (line.includes('|') && lines[i + 1]?.match(/^\s*\|?[\s:|-]+\|/)) {
      const head = line.split('|').map(c => c.trim()).filter(Boolean)
      i += 2
      const body = []
      while (i < lines.length && lines[i].includes('|')) {
        body.push(lines[i].split('|').map(c => c.trim()).filter(Boolean))
        i++
      }
      out.push(
        <div key={key++} className="my-2 overflow-x-auto">
          <table className="w-full text-2xs border border-line">
            <thead>
              <tr className="bg-raised">
                {head.map((h, j) => (
                  <th key={j} className="text-left px-2 py-1 font-medium text-dim
                                         border-b border-line">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.slice(0, 25).map((r, j) => (
                <tr key={j} className="border-b border-line/50">
                  {r.map((c, k) => (
                    <td key={k} className="px-2 py-1 text-faint">{inline(c, `${j}${k}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>)
      continue
    }

    // headings
    const h = line.match(/^(#{1,4})\s+(.*)$/)
    if (h) {
      const level = h[1].length
      const cls = level === 1
        ? 'text-sm font-medium text-ink mt-4 mb-1.5'
        : level === 2
          ? 'text-xs font-medium text-ink mt-3 mb-1'
          : 'text-2xs font-medium text-dim mt-2.5 mb-1'
      out.push(<div key={key++} className={cls}>{inline(h[2], key)}</div>)
      i++
      continue
    }

    // horizontal rule
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      out.push(<div key={key++} className="my-3 border-t border-line" />)
      i++
      continue
    }

    // list
    if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+\.\s+/.test(line)) {
      const items = []
      while (i < lines.length &&
             (/^\s*[-*+]\s+/.test(lines[i]) || /^\s*\d+\.\s+/.test(lines[i]))) {
        items.push(lines[i].replace(/^\s*(?:[-*+]|\d+\.)\s+/, ''))
        i++
      }
      out.push(
        <ul key={key++} className="my-1.5 space-y-0.5">
          {items.map((it, j) => (
            <li key={j} className="flex gap-2 text-2xs text-dim leading-relaxed">
              <span className="text-faint select-none">·</span>
              <span>{inline(it, `${key}${j}`)}</span>
            </li>
          ))}
        </ul>)
      continue
    }

    // blank
    if (!line.trim()) { i++; continue }

    // paragraph — gather until a blank line
    const buf = [line]
    i++
    while (i < lines.length && lines[i].trim() &&
           !/^(#{1,4}\s|```|\s*[-*+]\s|\s*\d+\.\s)/.test(lines[i]) &&
           !lines[i].includes('|')) {
      buf.push(lines[i]); i++
    }
    out.push(
      <p key={key++} className="my-1.5 text-2xs text-dim leading-relaxed">
        {inline(buf.join(' '), key)}
      </p>)
  }

  return <div className={className}>{out}</div>
}

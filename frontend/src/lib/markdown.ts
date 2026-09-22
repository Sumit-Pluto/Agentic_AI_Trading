function esc(s: unknown): string {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string)
}

/** Minimal, XSS-safe markdown -> HTML for assistant replies. HTML is escaped
 * FIRST, then a small subset is re-introduced: code, tables, headings,
 * bold/italic, bullet + numbered lists, line breaks. */
export function md(src?: string | null): string {
  let s = esc(src || '')
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>')
  s = s.replace(/(?:^[ \t]*\|.*\|[ \t]*\n?)+/gm, (block) => {
    const rows = block
      .trim()
      .split('\n')
      .map((r) => r.trim())
      .filter((r) => !/^\|[\s|:-]+\|?$/.test(r))
    if (!rows.length) return ''
    const cells = (r: string) => r.replace(/^\||\|$/g, '').split('|').map((c) => c.trim())
    return (
      "<table class='md-tbl'>" +
      rows
        .map((r, i) => {
          const tag = i === 0 ? 'th' : 'td'
          return '<tr>' + cells(r).map((c) => `<${tag}>${c}</${tag}>`).join('') + '</tr>'
        })
        .join('') +
      '</table>'
    )
  })
  s = s
    .replace(/^###\s+(.*)$/gm, '<h4>$1</h4>')
    .replace(/^##\s+(.*)$/gm, '<h3>$1</h3>')
    .replace(/^#\s+(.*)$/gm, '<h2>$1</h2>')
  s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>')
  s = s.replace(/(?:^[ \t]*[-*]\s+.*(?:\n|$))+/gm, (block) => {
    const items = block
      .trim()
      .split('\n')
      .map((l) => l.replace(/^[ \t]*[-*]\s+/, '').trim())
    return '<ul>' + items.map((i) => `<li>${i}</li>`).join('') + '</ul>'
  })
  s = s.replace(/(?:^[ \t]*\d+\.\s+.*(?:\n|$))+/gm, (block) => {
    const items = block
      .trim()
      .split('\n')
      .map((l) => l.replace(/^[ \t]*\d+\.\s+/, '').trim())
    return '<ol>' + items.map((i) => `<li>${i}</li>`).join('') + '</ol>'
  })
  s = s.replace(/\n{2,}/g, '<br><br>').replace(/\n/g, '<br>')
  s = s.replace(/(<\/(?:ul|ol|table|h2|h3|h4)>)(?:<br>)+/g, '$1').replace(/(?:<br>)+(<(?:ul|ol|table|h2|h3|h4))/g, '$1')
  return s
}

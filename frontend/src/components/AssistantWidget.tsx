import { useEffect, useRef, useState } from 'react'
import { jget, jpost } from '../lib/api'
import { md } from '../lib/markdown'

interface Turn {
  who: 'you' | 'assistant'
  html: string
  meta?: string
}

const SESSION_ID = 'ui-' + Date.now()
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

export function AssistantWidget() {
  const [open, setOpen] = useState(false)
  const [health, setHealth] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [msg, setMsg] = useState('')
  const [typing, setTyping] = useState<{ text: string; meta?: string } | null>(null)
  const logRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [turns, typing])

  async function toggle() {
    const next = !open
    setOpen(next)
    if (next) {
      try {
        const j = await jget<{ configured: boolean; model?: string }>('/api/assistant/health')
        setHealth(j.configured ? `· ${j.model || 'ready'}` : '· endpoint not configured (.env)')
      } catch {
        /* ignore */
      }
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }

  async function send() {
    const text = msg.trim()
    if (!text) return
    setMsg('')
    setTurns((t) => [...t, { who: 'you', html: text }])
    setTyping({ text: '…' })
    try {
      const j = await jpost<{ reply?: string; error?: string; tools_used?: string[]; mode?: string; disclaimer?: string }>('/api/assistant/chat', {
        session_id: SESSION_ID,
        message: text,
      })
      const meta = [
        j.tools_used?.length ? 'tools: ' + j.tools_used.join(', ') : '',
        j.mode && j.mode !== 'explanation' ? j.mode : '',
        j.disclaimer || '',
      ]
        .filter(Boolean)
        .join(' · ')
      const full = j.reply || j.error || '(no reply)'
      // progressive reveal: stream the already-validated reply, word by word
      const toks = full.split(/(\s+)/)
      let acc = ''
      for (let i = 0; i < toks.length; i++) {
        acc += toks[i]
        setTyping({ text: acc, meta })
        if (toks[i].trim()) await sleep(16)
      }
      setTurns((t) => [...t, { who: 'assistant', html: md(full), meta }])
      setTyping(null)
    } catch (e) {
      setTurns((t) => [...t, { who: 'assistant', html: `<span style="color:var(--red)">error: ${(e as Error).message}</span>` }])
      setTyping(null)
    }
  }

  return (
    <>
      <button
        onClick={toggle}
        title="Ask the assistant"
        className="fixed right-[22px] bottom-[22px] z-[60] w-[54px] h-[54px] rounded-full bg-accent text-white border-none text-[23px] leading-[54px] cursor-pointer shadow-lg hover:brightness-110"
      >
        💬
      </button>
      {open ? (
        <div className="fixed right-[22px] bottom-[88px] z-[60] w-[390px] max-w-[calc(100vw-44px)] h-[580px] max-h-[calc(100vh-130px)] flex flex-col bg-bg border border-line rounded-xl shadow-2xl overflow-hidden">
          <div className="flex items-center justify-between px-3.5 py-2.5 bg-bg2 border-b border-line font-semibold text-[13px]">
            <span>
              Assistant <span className="text-dim font-normal">{health}</span>
            </span>
            <button onClick={() => setOpen(false)} className="bg-transparent border-none text-[15px] cursor-pointer text-dim hover:text-fg px-1">
              ✕
            </button>
          </div>
          <div ref={logRef} className="flex-1 overflow-y-auto px-3.5 py-3 text-[13px]">
            {turns.map((t, i) => (
              <div key={i} className={`my-2.5 leading-relaxed ${t.who === 'you' ? 'text-dim' : ''}`}>
                <b>{t.who === 'you' ? 'You' : 'Assistant'}:</b>{' '}
                {t.who === 'you' ? t.html : <span className="md-body" dangerouslySetInnerHTML={{ __html: t.html }} />}
                {t.meta ? <div className="text-dim text-[11px] mt-1">{t.meta}</div> : null}
              </div>
            ))}
            {typing ? (
              <div className="my-2.5 leading-relaxed">
                <b>Assistant:</b> <span className="md-body" dangerouslySetInnerHTML={{ __html: md(typing.text) }} />
                {typing.meta ? <div className="text-dim text-[11px] mt-1">{typing.meta}</div> : null}
              </div>
            ) : null}
          </div>
          <div className="flex gap-1.5 p-2.5 border-t border-line">
            <input
              ref={inputRef}
              value={msg}
              onChange={(e) => setMsg(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && send()}
              placeholder="Ask about trades, signals, macro…"
              autoComplete="off"
              className="flex-1 bg-bg text-fg border border-line rounded-md px-2 py-1.5 text-xs outline-none focus:border-accent"
            />
            <button onClick={send} className="bg-bg3 border border-line rounded-md px-3 py-1.5 text-xs hover:border-accent hover:text-accent">
              Send
            </button>
          </div>
          <div className="text-dim text-[10.5px] px-3 pb-2.5">Explains what the system computed — not investment advice.</div>
        </div>
      ) : null}
    </>
  )
}

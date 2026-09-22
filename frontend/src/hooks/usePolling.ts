import { useEffect, useRef } from 'react'

/** Runs `fn` immediately and then every `ms` while `active` stays true. */
export function usePolling(fn: () => void, ms: number, active = true) {
  const fnRef = useRef(fn)

  useEffect(() => {
    fnRef.current = fn
  })

  useEffect(() => {
    if (!active) return
    fnRef.current()
    const id = setInterval(() => fnRef.current(), ms)
    return () => clearInterval(id)
  }, [ms, active])
}

/** Same as usePolling but skips ticks while the tab is hidden. */
export function useVisiblePolling(fn: () => void, ms: number, active = true) {
  usePolling(() => {
    if (document.visibilityState !== 'hidden') fn()
  }, ms, active)
}

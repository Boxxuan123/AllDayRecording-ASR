import { IS_MOCK } from './api'

export function watchProcessingEvents(onChange: () => void): () => void {
  if (IS_MOCK) return () => undefined
  let closed = false
  let source: EventSource | null = null
  let fallback: number | null = null

  function connect(): void {
    if (closed) return
    source = new EventSource('/api/v3/events/processing')
    source.addEventListener('processing', onChange)
    source.onerror = () => {
      source?.close()
      onChange()
      fallback = window.setTimeout(connect, 2500)
    }
  }

  connect()
  return () => {
    closed = true
    source?.close()
    if (fallback !== null) window.clearTimeout(fallback)
  }
}

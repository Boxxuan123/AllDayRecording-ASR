import { readonly, ref } from 'vue'

const audio = new Audio()
const activeMediaId = ref<string | null>(null)
const playing = ref(false)
const currentTimeMs = ref(0)
const rangeStartMs = ref(0)
const rangeEndMs = ref<number | null>(null)
let stopAt: number | null = null

audio.addEventListener('play', () => { playing.value = true })
audio.addEventListener('pause', () => { playing.value = false })
audio.addEventListener('timeupdate', () => {
  currentTimeMs.value = Math.round(audio.currentTime * 1000)
  if (stopAt !== null && audio.currentTime >= stopAt) stop()
})

export const mediaState = readonly({ activeMediaId, playing, currentTimeMs, rangeStartMs, rangeEndMs })

export function playRange(mediaId: string, startMs = 0, endMs?: number): void {
  const source = `/api/v3/media/${encodeURIComponent(mediaId)}`
  playSourceRange(mediaId, source, startMs, endMs)
}

export function playSessionRange(
  sessionId: string,
  sessionStartMs: number,
  sessionEndMs: number,
  positionMs = 0,
): void {
  const source = `/api/v3/recording-sessions/${encodeURIComponent(sessionId)}/audio?start_ms=${sessionStartMs}&end_ms=${sessionEndMs}`
  playSourceRange(
    `session:${sessionId}:${sessionStartMs}:${sessionEndMs}`,
    source,
    positionMs,
    sessionEndMs - sessionStartMs,
  )
}

function playSourceRange(sourceId: string, source: string, startMs = 0, endMs?: number): void {
  if (activeMediaId.value !== sourceId || audio.src !== new URL(source, window.location.href).href) {
    stop()
    audio.src = source
    activeMediaId.value = sourceId
  }
  audio.currentTime = startMs / 1000
  currentTimeMs.value = startMs
  rangeStartMs.value = startMs
  rangeEndMs.value = endMs ?? null
  stopAt = endMs === undefined ? null : endMs / 1000
  void audio.play()
}

export function seek(positionMs: number): void {
  const lower = rangeStartMs.value
  const upper = rangeEndMs.value ?? Number.POSITIVE_INFINITY
  const next = Math.min(Math.max(positionMs, lower), upper)
  audio.currentTime = next / 1000
  currentTimeMs.value = next
}

export function stop(): void {
  audio.pause()
  stopAt = null
}

export function release(): void {
  stop()
  audio.removeAttribute('src')
  audio.load()
  activeMediaId.value = null
  currentTimeMs.value = 0
  rangeStartMs.value = 0
  rangeEndMs.value = null
}

import { readonly, ref } from 'vue'

const audio = new Audio()
const activeMediaId = ref<string | null>(null)
const playing = ref(false)
const currentTimeMs = ref(0)
const rangeStartMs = ref(0)
const rangeEndMs = ref<number | null>(null)
const auditionGeneration = ref(0)
let cancelAudition: (() => void) | null = null
let stopAt: number | null = null

audio.addEventListener('play', () => { playing.value = true })
audio.addEventListener('pause', () => { playing.value = false })
audio.addEventListener('timeupdate', () => {
  currentTimeMs.value = Math.round(audio.currentTime * 1000)
  if (stopAt !== null && audio.currentTime >= stopAt) stop()
})

export const mediaState = readonly({ activeMediaId, playing, currentTimeMs, rangeStartMs, rangeEndMs, auditionGeneration })

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
  cancelAudition?.()
  auditionGeneration.value += 1
  const lower = rangeStartMs.value
  const upper = rangeEndMs.value ?? Number.POSITIVE_INFINITY
  const next = Math.min(Math.max(positionMs, lower), upper)
  audio.currentTime = next / 1000
  currentTimeMs.value = next
}

export function stop(): void {
  cancelAudition?.()
  auditionGeneration.value += 1
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

// Full sample playback has no seek path. Only the native ended event completes it.
export function playCompleteSample(blob: Blob, id: string): Promise<void> {
  stop()
  const url = URL.createObjectURL(blob)
  audio.src = url
  activeMediaId.value = id
  rangeStartMs.value = 0
  rangeEndMs.value = null
  currentTimeMs.value = 0
  return new Promise((resolve, reject) => {
    let settled = false
    const cleanup = () => {
      audio.removeEventListener('ended', ended)
      audio.removeEventListener('error', failed)
      audio.removeEventListener('pause', paused)
      cancelAudition = null
      URL.revokeObjectURL(url)
    }
    const ended = () => { if (settled) return; settled = true; cleanup(); resolve() }
    const failed = () => { if (settled) return; settled = true; cleanup(); reject(new Error('试听失败或中断，请重新完整播放')) }
    const paused = () => { if (!audio.ended) failed() }
    cancelAudition = failed
    audio.addEventListener('ended', ended)
    audio.addEventListener('error', failed)
    audio.addEventListener('pause', paused)
    audio.play().catch(failed)
  })
}

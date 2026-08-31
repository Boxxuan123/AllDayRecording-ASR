import { readonly, ref } from 'vue'

const audio = new Audio()
const activeMediaId = ref<string | null>(null)
const playing = ref(false)
let stopAt: number | null = null

audio.addEventListener('play', () => { playing.value = true })
audio.addEventListener('pause', () => { playing.value = false })
audio.addEventListener('timeupdate', () => {
  if (stopAt !== null && audio.currentTime >= stopAt) stop()
})

export const mediaState = readonly({ activeMediaId, playing })

export function playRange(mediaId: string, startMs = 0, endMs?: number): void {
  const source = `/api/v3/media/${encodeURIComponent(mediaId)}`
  if (activeMediaId.value !== mediaId || audio.src !== new URL(source, window.location.href).href) {
    stop()
    audio.src = source
    activeMediaId.value = mediaId
  }
  audio.currentTime = startMs / 1000
  stopAt = endMs === undefined ? null : endMs / 1000
  void audio.play()
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
}

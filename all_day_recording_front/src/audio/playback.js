let activeAudio = null
const coordinatedRoots = new WeakSet()

export function installExclusiveAudioPlayback(root = document) {
  if (coordinatedRoots.has(root)) return
  coordinatedRoots.add(root)
  root.addEventListener('play', (event) => {
    const nextAudio = event.target
    if (nextAudio?.tagName !== 'AUDIO') return
    const previousAudio = activeAudio
    activeAudio = nextAudio
    if (previousAudio && previousAudio !== nextAudio) previousAudio.pause()
    root.querySelectorAll('audio').forEach((audio) => {
      if (audio !== nextAudio && !audio.paused) audio.pause()
    })
  }, true)
}

export function pauseAllAudio(root = document) {
  const audios = new Set(root.querySelectorAll('audio'))
  if (activeAudio) audios.add(activeAudio)
  audios.forEach((audio) => audio.pause())
  activeAudio = null
}

export function waitForAudioMetadata(audio, timeoutMs = 8000) {
  if (audio.readyState >= 1 && Number.isFinite(audio.duration)) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => finish(new Error('音频加载超时')), timeoutMs)
    const finish = (error = null) => {
      window.clearTimeout(timeout)
      audio.removeEventListener('loadedmetadata', loaded)
      audio.removeEventListener('error', failed)
      if (error) reject(error)
      else resolve()
    }
    const loaded = () => finish()
    const failed = () => finish(new Error('音频元数据加载失败'))
    audio.addEventListener('loadedmetadata', loaded)
    audio.addEventListener('error', failed)
    audio.load()
  })
}

export function seekAudio(audio, seconds, timeoutMs = 5000) {
  const target = Math.max(0, Math.min(seconds, audio.duration || seconds))
  audio.currentTime = target
  if (!audio.seeking && Math.abs(audio.currentTime - target) < 0.05) return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => finish(new Error('音频跳转超时')), timeoutMs)
    const finish = (error = null) => {
      window.clearTimeout(timeout)
      audio.removeEventListener('seeked', completed)
      audio.removeEventListener('error', failed)
      if (error) reject(error)
      else resolve()
    }
    const completed = () => finish()
    const failed = () => finish(new Error('无法跳转到所选时间'))
    audio.addEventListener('seeked', completed)
    audio.addEventListener('error', failed)
  })
}

export function clampAudioRange(startMs, endMs, durationMs) {
  const start = Math.max(0, Math.min(Number(startMs) || 0, durationMs))
  const end = Math.max(start, Math.min(Number(endMs) || start, durationMs))
  return { startMs: start, endMs: end }
}

export function releaseAudio(audio) {
  if (activeAudio === audio) activeAudio = null
  audio.pause()
  audio.removeAttribute('src')
  audio.load()
}

export function releaseAudioWithin(root) {
  root?.querySelectorAll('audio').forEach((audio) => releaseAudio(audio))
}

export function replaceAudioSource(audio, source) {
  if (activeAudio === audio) activeAudio = null
  audio.pause()
  audio.src = source
  audio.load()
}

export function audioRangeSource(source, startMs, endMs, version = 3) {
  const absolute = /^[a-z][a-z\d+.-]*:/i.test(source)
  const url = new URL(source, 'http://localhost')
  url.searchParams.set('start_ms', String(Math.round(startMs)))
  url.searchParams.set('end_ms', String(Math.round(endMs)))
  url.searchParams.set('v', String(version))
  return absolute ? url.href : `${url.pathname}${url.search}${url.hash}`
}

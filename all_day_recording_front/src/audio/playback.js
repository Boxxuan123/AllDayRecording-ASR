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

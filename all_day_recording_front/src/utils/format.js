export function formatDuration(milliseconds) {
  const total = Math.max(0, Math.round(milliseconds / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours) return `${hours}h ${minutes}m ${seconds}s`
  return `${minutes}m ${seconds}s`
}

export function formatOffset(milliseconds) {
  const total = Math.max(0, Math.round(milliseconds / 1000))
  const hours = Math.floor(total / 3600).toString().padStart(2, '0')
  const minutes = Math.floor((total % 3600) / 60).toString().padStart(2, '0')
  const seconds = (total % 60).toString().padStart(2, '0')
  return `${hours}:${minutes}:${seconds}`
}

export function formatDate(value) {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(parsed)
}

export function metric(value) {
  return value === null || value === undefined ? 'N/A' : Number(value).toFixed(4)
}

export function workflowStateLabel(value) {
  const labels = {
    semantic_ready: '证据已就绪',
    semantic_ready_empty: '无文字证据',
    semantic_ready_needs_review: '待人工检查',
    semantic_ready_empty_needs_review: '无文字 · 待检查',
    asr_completed: 'ASR 已完成',
    diarization_completed: '说话人已完成',
    failed: '运行失败',
  }
  return labels[value] || value || '未运行 V2'
}

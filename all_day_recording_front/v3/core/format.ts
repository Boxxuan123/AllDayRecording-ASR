const statusLabels: Record<string, string> = {
  queued: '排队中', running: '处理中', waiting_review: '等待审核', succeeded: '已完成',
  failed_retryable: '可重试失败', failed_final: '最终失败', cancel_requested: '正在取消',
  cancelled: '已取消', stale: '已失效', pending: '等待中', active: '可用',
  available: '可用', backup_required: '等待备份', processing: '处理中', ready: '已就绪',
  verified: '已核验', failed: '失败', lost_lease: '租约丢失',
}

export function statusLabel(value: string | null): string {
  return value ? statusLabels[value] ?? value : '尚未开始'
}

export function formatDate(value: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

export function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(value))
}

export function formatDuration(value: number): string {
  const seconds = Math.floor(value / 1000)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return hours ? `${hours} 小时 ${minutes} 分` : `${minutes} 分 ${seconds % 60} 秒`
}

export function formatOffset(value: number): string {
  const seconds = Math.floor(value / 1000)
  return [Math.floor(seconds / 3600), Math.floor((seconds % 3600) / 60), seconds % 60]
    .map((part) => String(part).padStart(2, '0')).join(':')
}

export function formatBytes(value: number | null): string {
  if (value === null) return '—'
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`
  return `${(value / 1024 ** 3).toFixed(1)} GB`
}

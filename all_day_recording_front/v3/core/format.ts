const statusLabels: Record<string, string> = {
  queued: '排队中', running: '处理中', waiting_review: '等待审核', succeeded: '已完成',
  failed_retryable: '可重试失败', failed_final: '最终失败', cancel_requested: '正在取消',
  cancelled: '已取消', stale: '已失效', pending: '等待中', active: '可用',
  available: '可用', backup_required: '等待备份', processing: '处理中', ready: '已就绪',
  verified: '已核验', failed: '失败', lost_lease: '租约丢失',
}

const stageLabels: Record<string, string> = {
  ingest: '接收原音',
  ingest_verified: '原音接收已核验',
  backup_admission: '检查独立备份',
  backup_admitted: '备份准入完成',
  window_plan: '规划处理分段',
  speech_gate: '检测有效语音',
  asr_and_alignment: '转写与时间对齐',
  diarization: '区分说话人',
  utterance_projection: '生成对话时间线',
  semantic_evidence_optional: '生成语义证据',
  speaker_identity: '识别说话人',
  reminder_generation: '生成提醒建议',
  daily_insight: '生成每日总结',
  mobile_projection: '同步手机端视图',
  deduplicated: '重复录音已归档',
  completed: '全部处理完成',
  complete: '全部处理完成',
  processing: '处理中',
}

const artifactKindLabels: Record<string, string> = {
  v3_semantic_input: '语义处理输入',
  v3_transcript_evidence: '转写证据',
  v3_diarization_evidence: '说话人区分证据',
  v3_asr_evidence: '语音识别证据',
}

const producerLabels: Record<string, string> = {
  'v3-native-semantic-input': '语义输入生成器',
  'v3-native-transcript-projection': '转写证据生成器',
  'v3-native-diarization': '说话人区分模型',
  'v3-native-asr': '语音识别模型',
  'v3-native-model-pipeline': '本地处理流水线',
}

export function statusLabel(value: string | null): string {
  return value ? statusLabels[value] ?? value : '尚未开始'
}

export function stageLabel(value: string | null): string {
  return value ? stageLabels[value] ?? '处理中' : '尚未开始'
}

export function artifactKindLabel(value: string): string {
  return artifactKindLabels[value] ?? '处理证据'
}

export function producerLabel(value: string): string {
  return producerLabels[value] ?? '本地处理流水线'
}

export function formatDate(value: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

export function formatTimezone(value: string): string {
  if (['CST', 'Asia/Shanghai', 'Asia/Singapore'].includes(value)) return 'UTC+8'
  return value.replaceAll('_', ' ')
}

export function sessionTitle(value: string): string {
  const hour = new Date(value).getHours()
  if (hour < 6) return '凌晨录音'
  if (hour < 11) return '上午录音'
  if (hour < 14) return '午间录音'
  if (hour < 18) return '下午录音'
  return '晚间录音'
}

export function shortSessionId(value: string): string {
  return value.length > 16 ? `${value.slice(0, 4)}…${value.slice(-10)}` : value
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

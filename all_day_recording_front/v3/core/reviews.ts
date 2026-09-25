import type { ReviewItem, ReviewKind, VoicePrototypeCandidate } from './types'

export type VoiceReviewLane = 'primary' | 'training'
export type ReviewCategory = 'action' | 'memory' | 'identity' | 'risk'

export const reviewCategories: ReviewCategory[] = ['action', 'memory', 'identity', 'risk']

export const reviewCategoryLabels: Record<ReviewCategory, string> = {
  action: '行动确认',
  memory: '记忆确认',
  identity: '身份确认',
  risk: '风险确认',
}

export const reviewKindLabels: Record<ReviewKind, string> = {
  workflow_failure: '流程故障',
  processing_gate: '风险确认',
  reminder: '行动确认',
  person_memory: '记忆确认',
  voice_identity: '身份确认',
  knowledge_proposal: '记忆确认',
}

export const VOICE_TRAINING_MIN_SCORE = 0.70
export const VOICE_TRAINING_MIN_MARGIN = 0.10

export function voiceReviewLane(candidate: VoicePrototypeCandidate): VoiceReviewLane | null {
  // Mirror the existing server review lane for authoritative manual selections.
  if (candidate.human_selection === true || candidate.human_selection === 1) return 'primary'
  if (candidate.decision_tier === 'suggested') return 'primary'
  if (candidate.decision_tier !== 'no_known_match') return null
  if (candidate.best_score === null || candidate.best_score < VOICE_TRAINING_MIN_SCORE) return null
  if (candidate.score_margin !== null && candidate.score_margin < VOICE_TRAINING_MIN_MARGIN) return null
  return 'training'
}

export function reviewCategory(item: ReviewItem): ReviewCategory {
  if (item.kind === 'reminder') return 'action'
  if (item.kind === 'person_memory' || item.kind === 'knowledge_proposal') return 'memory'
  if (item.kind === 'voice_identity') return 'identity'
  return 'risk'
}

export function reviewDecisionReason(item: ReviewItem): string {
  if (item.kind === 'reminder') return '这不是一条可以安全直接执行的明确指令，确认后才会改变提醒状态。'
  if (item.kind === 'person_memory') return '这条内容会进入长期可信记忆，之后可能影响人物理解和行动建议。'
  if (item.kind === 'knowledge_proposal') return '这项内容会成为可跨天使用的可信记录，而不只是一次对话中的推断。'
  if (item.kind === 'voice_identity') return item.context.voice_mode === 'speaker_discovery'
    ? '同一未知声音已跨录音出现，只有你能判断是否需要建立人物。'
    : '只有逐个试听并确认的声音样本，才会用于后续人物识别。'
  return '继续后可能产生不可逆的数据影响，需要由你决定。'
}

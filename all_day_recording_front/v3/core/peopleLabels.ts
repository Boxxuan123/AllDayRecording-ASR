import type { PersonMemory, PersonMemoryKind, VoicePrototypeCandidate } from './types'

export function kindLabel(kind: PersonMemoryKind): string {
  return { stable_fact: '稳定事实', preference: '偏好', short_term_state: '短期状态', plan: '计划', commitment: '承诺', model_observation: '模型观察' }[kind]
}

export function maturityLabel(value: string | null): string {
  if (value === 'seed') return '待积累'
  if (value === 'learning') return '学习中'
  if (value === 'calibrated') return '已校准'
  if (value === 'suspended') return '已暂停'
  return '不适用'
}

export function matchTierLabel(value: VoicePrototypeCandidate['decision_tier']): string {
  if (value === 'insufficient_evidence') return '证据不足'
  if (value === 'auto_matched') return '达到自动档'
  if (value === 'suggested') return '建议核对'
  if (value === 'no_known_match') return '未达已知人物阈值'
  return '新人物待指定'
}

export function statusLabel(memory: PersonMemory): string {
  return { active: '当前有效', expired: '已过期', retracted: '已撤回' }[memory.status]
}

export function confirmationLabel(memory: PersonMemory): string {
  return { confirmed: '已确认', unconfirmed: '待核对', inferred: '模型推断' }[memory.confirmation_status]
}

export function sourceLabel(memory: PersonMemory): string {
  return { human: '人工维护', event_projection: '事件自动整理', model: '模型观察' }[memory.source]
}

export function roleLabel(memory: PersonMemory): string {
  return memory.details.person_role === 'actor' ? '本人陈述/主体' : memory.details.person_role === 'related' ? '关联人物' : '人工记录'
}


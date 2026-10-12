export const unifiedSections = [
  { key: 'people', label: '今天与重要人物的互动' },
  { key: 'activity', label: '今天涉及的重要事情和讨论' },
  { key: 'followup', label: '承诺、进展与待跟进' },
]
export const coverageExplanation = '聊天选取数／缓存数只表示输入筛选范围，不是召回率；未入选记录不等于没有价值。'
export function recordingIdentity(e: { participant?: { kind?: string; label?: string; status?: string } }): string {
  return !e.participant || e.participant.kind === 'unknown' || e.participant.status === 'unresolved'
    ? '说话人身份未确认' : e.participant.label ?? '说话人身份未确认'
}

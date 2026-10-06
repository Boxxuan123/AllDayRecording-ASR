const labels = { decision: '明确决定', commitment: '明确承诺', completion: '明确完成表述' }

export function dailyOutcomeText(event) {
  const outcome = event.outcome
  if (!outcome || typeof outcome !== 'object' || !Object.hasOwn(labels, outcome.kind) ||
      typeof outcome.quote !== 'string' || !outcome.quote.trim() ||
      !Array.isArray(outcome.evidence_utterance_ids) || !outcome.evidence_utterance_ids.length) return ''
  const ids = new Set(event.evidence_snapshots.map(row => row.utterance_id))
  if (!outcome.evidence_utterance_ids.every(id => typeof id === 'string' && ids.has(id))) return ''
  return `${labels[outcome.kind]}：${outcome.quote}`
}

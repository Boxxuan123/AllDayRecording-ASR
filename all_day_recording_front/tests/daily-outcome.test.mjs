import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dailyOutcomeText } from '../v3/core/dailyOutcome.js'

const event = { title: '匿名技术讨论', summary: '可能改方案？', evidence_snapshots: [{ utterance_id: 'source-a' }] }
for (const [kind, label] of [['decision', '明确决定'], ['commitment', '明确承诺'], ['completion', '明确完成表述']]) {
  const original = { ...event, outcome: { kind, quote: '已确认使用方案乙', evidence_utterance_ids: ['source-a'] } }
  const snapshot = JSON.stringify(original)
  assert.equal(dailyOutcomeText(original), `${label}：已确认使用方案乙`)
  assert.equal(JSON.stringify(original), snapshot)
}
for (const outcome of [null, undefined, 'not_inferred', { kind: 'suggestion', quote: '可以试试方案乙', evidence_utterance_ids: ['source-a'] },
  { kind: 'decision', quote: '已确认', evidence_utterance_ids: ['unknown-source'] },
  { kind: 'decision', quote: '已确认', evidence_utterance_ids: [] }]) {
  assert.equal(dailyOutcomeText({ ...event, outcome }), '')
}
const view = readFileSync(new URL('../v3/views/InsightsView.vue', import.meta.url), 'utf8')
assert.ok(view.includes('v-if="dailyOutcomeText(event)"'))
assert.ok(view.includes('{{ dailyOutcomeText(event) }}'))
console.log('PASS existing outcome display, source references, null/suggestion guards, immutable input and Vue wiring')

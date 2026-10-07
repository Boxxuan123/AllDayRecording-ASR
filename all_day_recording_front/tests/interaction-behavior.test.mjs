import assert from 'node:assert/strict'
import { createRenderer, defineComponent, h, nextTick, ref } from 'vue'
import { useQuery, invalidateQuery } from '../v3/core/query.ts'
import { voiceReviewLane, reviewCategory, reviewDecisionReason } from '../v3/core/reviews.ts'

// This exercises the actual query state machine used by views, including
// shared requests, cache invalidation, and cancellation when a page closes.
const renderer = createRenderer({
  createElement: tag => ({ tag, children: [] }),
  createText: text => ({ text }),
  createComment: text => ({ text }),
  setText: (node, text) => { node.text = text },
  setElementText: (node, text) => { node.text = text },
  patchProp: () => undefined,
  insert: (child, parent) => { parent.children.push(child) },
  remove: () => undefined,
  parentNode: () => null,
  nextSibling: () => null,
})
const online = ref(true)
let release
let calls = 0
let signal
let first
let second
const app = renderer.createApp(defineComponent({ setup() {
  const loader = async (currentSignal) => {
    calls += 1
    signal = currentSignal
    return new Promise(resolve => { release = resolve })
  }
  first = useQuery('behavior/shared', loader, () => online.value)
  second = useQuery('behavior/shared', loader, () => online.value)
  return () => h('div')
} }))
app.mount({ children: [] })
const pair = Promise.all([first.refresh(true), second.refresh(true)])
assert.equal(calls, 1)
assert.equal(first.loading.value, true)
release({ revision: 1 })
await pair
assert.equal(first.data.value.revision, 1)
assert.equal(second.data.value.revision, 1)
assert.equal(first.loading.value, false)

let staleRelease
const staleApp = renderer.createApp(defineComponent({ setup() {
  first = useQuery('behavior/stale', async (currentSignal) => {
    signal = currentSignal
    return new Promise(resolve => { staleRelease = resolve })
  }, () => online.value)
  return () => h('div')
} }))
staleApp.mount({ children: [] })
const pending = first.refresh(true)
invalidateQuery('behavior/stale')
assert.equal(signal.aborted, true)
staleRelease({ revision: 2 })
await pending
assert.equal(first.data.value, null, 'invalidated response must not overwrite newer state')
online.value = false
await nextTick()
app.unmount()
staleApp.unmount()

// Action review policy is a user interaction boundary. Automatic suggestions
// cannot silently enter the training lane below confidence or margin limits.
const candidate = { decision_tier: 'no_known_match', human_selection: false,
  best_score: 0.81, score_margin: 0.11 }
assert.equal(voiceReviewLane(candidate), 'training')
assert.equal(voiceReviewLane({ ...candidate, score_margin: 0.09 }), null)
assert.equal(voiceReviewLane({ ...candidate, best_score: 0.69 }), null)
assert.equal(voiceReviewLane({ ...candidate, human_selection: true }), 'primary')
assert.equal(reviewCategory({ kind: 'reminder' }), 'action')
assert.equal(reviewCategory({ kind: 'voice_identity' }), 'identity')
assert.match(reviewDecisionReason({ kind: 'reminder' }), /确认后/)
console.log('PASS shared query state, invalidation/cancellation, and review action policy')

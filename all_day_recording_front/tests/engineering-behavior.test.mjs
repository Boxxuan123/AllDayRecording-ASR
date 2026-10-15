import assert from 'node:assert/strict'
import { createRenderer, defineComponent, h, nextTick } from 'vue'
import { useQuery, invalidateQueriesMatching, invalidateEvidenceQueries } from '../v3/core/query.ts'

globalThis.window = { location: { pathname: '/reviews', search: '' }, addEventListener() {}, history: { pushState() {} }, scrollTo() {} }
Object.defineProperty(globalThis, 'navigator', { value: { onLine: true }, configurable: true })
globalThis.Audio = class { addEventListener() {}; pause() {}; removeAttribute() {}; load() {} }

const renderer = createRenderer({
  createElement: tag => ({ tag, children: [], props: {} }),
  createText: text => ({ text }), createComment: text => ({ text }),
  setText: (node, text) => { node.text = text },
  setElementText: (node, text) => { node.text = text; node.children = [] },
  patchProp: (node, key, previous, value) => { node.props[key] = value },
  insert(child, parent, anchor) {
    if (child.parent) {
      const old = child.parent.children.indexOf(child)
      if (old >= 0) child.parent.children.splice(old, 1)
    }
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(child); else parent.children.splice(index, 0, child)
    child.parent = parent
  },
  remove(node) { if (node.parent) { const index = node.parent.children.indexOf(node); if (index >= 0) node.parent.children.splice(index, 1) } },
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
})
const flush = async () => { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
function flatten(node) { return [node, ...(node.children ?? []).flatMap(flatten)] }
function text(node) { return flatten(node).map(n => n.text ?? '').join('') }

const { desktopApi } = await import('../v3/core/api.ts')
const { default: ReviewsView } = await import('../v3/views/ReviewsView.vue')
let fail = true
let confirms = 0
desktopApi.reviews = async () => ({ items: [{ review_id: 'review-synthetic', source_id: 'candidate', kind: 'reminder',
  title: 'Synthetic reminder survives failure', priority: 'normal', context: {}, session_id: 'session', person_id: null,
  evidence_count: 1, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z' }], next_cursor: null })
desktopApi.reminderCandidates = async () => {
  if (fail) throw new Error('injected candidate transport failure')
  return { items: [{ candidate_id: 'candidate', evidence: [], status: 'pending_confirmation' }] }
}
desktopApi.confirmReminder = async () => { confirms += 1; return {} }
invalidateQueriesMatching(() => true)
const root = { children: [] }
const app = renderer.createApp(ReviewsView)
app.mount(root)
await flush()
assert.match(text(root), /Synthetic reminder survives failure/)
assert.match(text(root), /候选详情加载失败/)
let confirm = flatten(root).find(n => n.tag === 'button' && text(n).includes('确认执行'))
assert.equal(confirm.props.disabled, true)
fail = false
const retry = flatten(root).find(n => n.tag === 'button' && text(n).includes('重试候选详情'))
await retry.props.onClick()
await flush()
confirm = flatten(root).find(n => n.tag === 'button' && text(n).includes('确认执行'))
assert.equal(confirm.props.disabled, false)
await confirm.props.onClick()
await flush()
assert.equal(confirms, 1)
app.unmount()

// Mount/unmount the actual query hook for TTL, capacity and shared cancellation.
async function read(key, loader) {
  let query
  const mounted = renderer.createApp(defineComponent({ setup() { query = useQuery(key, loader); return () => h('div') } }))
  mounted.mount({ children: [] })
  await flush()
  return { query, mounted }
}
invalidateQueriesMatching(() => true)
let calls = 0
for (let i = 0; i < 130; i++) {
  const instance = await read(`capacity:${i}`, async () => { calls += 1; return i })
  instance.mounted.unmount()
}
const evicted = await read('capacity:0', async () => { calls += 1; return 'reloaded' })
assert.equal(evicted.query.data.value, 'reloaded')
assert.equal(calls, 131)
evicted.mounted.unmount()
const clock = Date.now
Date.now = () => clock() + 31000
try {
  const expired = await read('capacity:129', async () => 'expired-refresh')
  assert.equal(expired.query.data.value, 'expired-refresh')
  expired.mounted.unmount()
} finally { Date.now = clock }
let release, requestSignal
const loader = signal => { requestSignal = signal; return new Promise(resolve => { release = resolve }) }
const first = await read('shared-live', loader)
const second = await read('shared-live', loader)
first.mounted.unmount()
assert.equal(requestSignal.aborted, false)
release('shared-result')
await flush()
assert.equal(second.query.data.value, 'shared-result')
second.mounted.unmount()

// After an evidence edit, returning to each dependent view must fetch a new
// value, while the other recording's cache remains reusable.
const affected = ['overview', 'recordings', 'reviews', 'review-voice-candidates', 'review-reminder-candidates',
  'people-voice', 'session-overview:edited', 'session-brief:edited', 'session-timeline:edited']
for (const key of [...affected, 'session-overview:other']) {
  const mounted = await read(key, async () => 'revision-1')
  mounted.mounted.unmount()
}
invalidateEvidenceQueries('edited')
for (const key of affected) {
  const mounted = await read(key, async () => 'revision-2')
  assert.equal(mounted.query.data.value, 'revision-2', key)
  mounted.mounted.unmount()
}
const other = await read('session-overview:other', async () => assert.fail('unrelated cache invalidated'))
assert.equal(other.query.data.value, 'revision-1')
other.mounted.unmount()
console.log('PASS actual ReviewsView partial failure/retry/action availability and cache TTL/capacity/shared lifecycle')

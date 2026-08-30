import assert from 'node:assert/strict'

import { clampAudioRange } from '../src/audio/playback.js'
import { resetSessionWorkspace, state } from '../src/state/workspace.js'
import {
  formatDuration,
  formatOffset,
  metric,
  workflowStateLabel,
} from '../src/utils/format.js'

const cases = []

function test(name, callback) {
  cases.push({ name, callback })
}

test('formats durations and offsets at stable boundaries', () => {
  assert.equal(formatDuration(0), '0m 0s')
  assert.equal(formatDuration(3_661_000), '1h 1m 1s')
  assert.equal(formatOffset(3_661_000), '01:01:01')
})

test('formats metrics and workflow states without changing API values', () => {
  assert.equal(metric(null), 'N/A')
  assert.equal(metric(0.123456), '0.1235')
  assert.equal(workflowStateLabel('semantic_ready'), '证据已就绪')
  assert.equal(workflowStateLabel('future_state'), 'future_state')
})

test('clamps an audio selection into the available interval', () => {
  assert.deepEqual(clampAudioRange(-500, 15_000, 10_000), {
    startMs: 0,
    endMs: 10_000,
  })
  assert.deepEqual(clampAudioRange(8_000, 2_000, 10_000), {
    startMs: 8_000,
    endMs: 8_000,
  })
})

test('session reset preserves navigation and loaded session identity', () => {
  state.sessionId = 42
  state.activeView = 'semantic'
  state.evaluationName = 'baseline'
  state.timeline = { available: true }
  state.timelineSelectedId = 7
  state.page = 4

  resetSessionWorkspace()

  assert.equal(state.sessionId, 42)
  assert.equal(state.activeView, 'semantic')
  assert.equal(state.evaluationName, null)
  assert.equal(state.timeline, null)
  assert.equal(state.timelineSelectedId, null)
  assert.equal(state.page, 1)
})

for (const { name, callback } of cases) {
  callback()
  console.log(`ok - ${name}`)
}

console.log(`${cases.length} frontend unit tests passed`)

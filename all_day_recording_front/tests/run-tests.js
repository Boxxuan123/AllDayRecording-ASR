import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  audioRangeSource,
  clampAudioRange,
  installExclusiveAudioPlayback,
  pauseAllAudio,
  releaseAudio,
  replaceAudioSource,
} from '../src/audio/playback.js'
import { resetSessionWorkspace, state } from '../src/state/workspace.js'
import {
  formatDuration,
  formatOffset,
  formatRecordingTime,
  metric,
  workflowStateLabel,
} from '../src/utils/format.js'
import { sessionOptionLabel } from '../src/views/sessions.js'
import { decodeV3ContractFixture } from '../src/v3/contracts.js'
import { isV3Enabled } from '../src/v3/feature.js'
import { matchV3Route } from '../v3/core/routeMatch.js'

const cases = []

function test(name, callback) {
  cases.push({ name, callback })
}

test('formats durations and offsets at stable boundaries', () => {
  assert.equal(formatDuration(0), '0m 0s')
  assert.equal(formatDuration(3_661_000), '1h 1m 1s')
  assert.equal(formatOffset(3_661_000), '01:01:01')
  assert.equal(
    formatRecordingTime('2026-08-31T01:05:00Z', 'Asia/Singapore'),
    '2026/08/31 09:05',
  )
})

test('labels recording choices by capture time instead of filename', () => {
  const label = sessionOptionLabel({
    id: 1160,
    source_name: 'pcm_gap_test_1788083609984',
    recorded_at: '2026-08-31T01:05:00Z',
    timezone: 'Asia/Singapore',
    duration_ms: 1_003_120,
    workflow_state: null,
  })
  assert.equal(label, '2026/08/31 09:05 · 16m 43s · S1160 · 未运行 V2')
  assert.equal(label.includes('pcm_gap_test'), false)
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

test('releases audio before replacing a timeline candidate', () => {
  const calls = []
  const audio = {
    pause() { calls.push('pause') },
    removeAttribute(name) { calls.push(`remove:${name}`) },
    load() { calls.push('load') },
  }

  releaseAudio(audio)

  assert.deepEqual(calls, ['pause', 'remove:src', 'load'])
})

test('keeps playback exclusive across every audio player', () => {
  const listeners = {}
  const first = {
    tagName: 'AUDIO',
    paused: false,
    pause() { this.paused = true },
  }
  const second = {
    tagName: 'AUDIO',
    paused: false,
    pause() { this.paused = true },
  }
  const root = {
    addEventListener(name, callback) { listeners[name] = callback },
    querySelectorAll() { return [first, second] },
  }

  installExclusiveAudioPlayback(root)
  listeners.play({target: first})
  assert.equal(first.paused, false)
  assert.equal(second.paused, true)

  second.paused = false
  listeners.play({target: second})
  assert.equal(first.paused, true)
  assert.equal(second.paused, false)

  pauseAllAudio(root)
  assert.equal(second.paused, true)
})

test('replaces an audio source only after stopping current playback', () => {
  const calls = []
  const audio = {
    src: 'old.wav',
    pause() { calls.push('pause') },
    load() { calls.push(`load:${this.src}`) },
  }

  replaceAudioSource(audio, 'new.wav')

  assert.deepEqual(calls, ['pause', 'load:new.wav'])
})

test('builds a distinct exact source for every labeled audio range', () => {
  const base = '/api/speaker-timeline/audio?session_id=1160&start_ms=624658&end_ms=633738&v=2'
  const first = audioRangeSource(base, 625_000, 628_000)
  const second = audioRangeSource(base, 631_000, 633_000)

  assert.equal(first, '/api/speaker-timeline/audio?session_id=1160&start_ms=625000&end_ms=628000&v=3')
  assert.equal(second, '/api/speaker-timeline/audio?session_id=1160&start_ms=631000&end_ms=633000&v=3')
  assert.notEqual(first, second)
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

test('decodes the canonical V3 fixture and degrades future enums', () => {
  const fixtureUrl = new URL(
    '../../contracts/v3/fixtures/core-resources.json',
    import.meta.url,
  )
  const forwardUrl = new URL(
    '../../contracts/v3/fixtures/forward-enums.json',
    import.meta.url,
  )
  for (const url of [fixtureUrl, forwardUrl]) {
    const fixture = JSON.parse(readFileSync(url, 'utf8'))
    assert.deepEqual(decodeV3ContractFixture(fixture), fixture.expected_decode)
  }
})

test('enables the V3 frontend by default with an explicit rollback switch', () => {
  assert.equal(isV3Enabled(undefined), true)
  assert.equal(isV3Enabled('0'), false)
  assert.equal(isV3Enabled('true'), true)
})

test('restores a nested V3 recording route and its selected tab', () => {
  assert.deepEqual(
    matchV3Route('/recordings/session%20one', '?tab=evidence'),
    {
      name: 'session',
      path: '/recordings/session%20one',
      sessionId: 'session one',
      tab: 'evidence',
    },
  )
  assert.deepEqual(matchV3Route('/unknown'), {
    name: 'overview',
    path: '/',
    sessionId: null,
    tab: null,
  })
  assert.equal(matchV3Route('/reminders').name, 'reminders')
})

test('keeps V3 independent from the legacy DOM workspace controller', () => {
  const app = readFileSync(new URL('../v3/App.vue', import.meta.url), 'utf8')
  const shell = readFileSync(
    new URL('../v3/components/AppShell.vue', import.meta.url),
    'utf8',
  )
  const media = readFileSync(new URL('../v3/core/media.ts', import.meta.url), 'utf8')
  const pageState = readFileSync(
    new URL('../v3/components/PageState.vue', import.meta.url),
    'utf8',
  )
  assert.equal(app.includes('workspace/controller'), false)
  assert.equal(app.includes('getElementById'), false)
  assert.equal(shell.includes("path: '/lab'"), false)
  assert.equal((media.match(/new Audio\(/g) ?? []).length, 1)
  assert.equal(media.includes('audio.src ='), true)
  assert.equal(media.includes("audio.removeAttribute('src')"), true)
  assert.equal(pageState.includes('设备处于离线状态'), true)
})

for (const { name, callback } of cases) {
  callback()
  console.log(`ok - ${name}`)
}

console.log(`${cases.length} frontend unit tests passed`)

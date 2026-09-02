import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { matchV3Route } from '../v3/core/routeMatch.js'

const cases = []

function test(name, callback) {
  cases.push({ name, callback })
}

test('restores nested recording routes and selected tabs', () => {
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
  assert.equal(matchV3Route('/people').name, 'people')
})

test('keeps the V3 frontend on one shared audio controller', () => {
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
  assert.equal(app.includes('getElementById'), false)
  assert.equal(shell.includes("path: '/lab'"), false)
  assert.equal((media.match(/new Audio\(/g) ?? []).length, 1)
  assert.equal(media.includes('audio.src ='), true)
  assert.equal(media.includes('export function seek'), true)
  assert.equal(media.includes('export function playSessionRange'), true)
  assert.equal(media.includes('/audio?start_ms='), true)
  assert.equal(media.includes('currentTimeMs'), true)
  assert.equal(media.includes("audio.removeAttribute('src')"), true)
  assert.equal(pageState.includes('设备处于离线状态'), true)
})

test('keeps recording discovery and long timelines usable', () => {
  const recordings = readFileSync(
    new URL('../v3/views/RecordingsView.vue', import.meta.url),
    'utf8',
  )
  const detail = readFileSync(
    new URL('../v3/views/SessionDetailView.vue', import.meta.url),
    'utf8',
  )
  assert.equal(recordings.includes('搜索日期、转写、说话人或 Session ID'), true)
  assert.equal(recordings.includes('copySessionId'), true)
  assert.equal(detail.includes('visibleTimeline'), true)
  assert.equal(detail.includes('timelineLimit += 80'), true)
  assert.equal(detail.includes('buildTranscriptTurns'), true)
  assert.equal(detail.includes('sessionRangeAvailable'), true)
  assert.equal(detail.includes('utteranceSegment'), false)
  assert.equal(detail.includes('sameSegment'), false)
  assert.equal(detail.includes('逐句校正'), true)
  assert.equal(detail.includes("timelineDensity = ref<'compact' | 'comfortable'>"), true)
  assert.equal(detail.includes('activePlayback'), true)
  assert.equal(detail.includes('showSegmentInTimeline'), true)
  assert.equal(detail.includes('复制摘要'), true)
  assert.equal(detail.includes('backup_admitted'), false)
})

test('keeps the review inbox meaningful, filterable, and directly actionable', () => {
  const reviews = readFileSync(
    new URL('../v3/views/ReviewsView.vue', import.meta.url),
    'utf8',
  )
  const people = readFileSync(
    new URL('../v3/views/PeopleView.vue', import.meta.url),
    'utf8',
  )
  const voiceReviewActions = readFileSync(
    new URL('../v3/components/VoiceReviewActions.vue', import.meta.url),
    'utf8',
  )
  const reviewPolicy = readFileSync(
    new URL('../v3/core/reviews.ts', import.meta.url),
    'utf8',
  )
  assert.equal(reviewPolicy.includes("type ReviewCategory = 'action' | 'memory' | 'identity' | 'risk'"), true)
  assert.equal(reviews.includes("type ReviewGroupMode = 'session' | 'person'"), true)
  assert.equal(reviews.includes('@click="setKindFilter(kind)"'), true)
  assert.equal(reviews.includes('v-for="kind in activeCategories"'), true)
  assert.equal(reviews.includes("suggested: '建议核对'"), true)
  assert.equal(reviews.includes("no_known_match: '未达阈值'"), true)
  assert.equal(reviews.includes('匹配度 ${score}'), true)
  assert.equal(reviews.includes('reviewVoiceCandidate(item, candidate, decision)'), true)
  assert.equal(reviews.includes('Promise.all(candidates.map'), false)
  assert.equal(reviews.includes('确认整组'), false)
  assert.equal(voiceReviewActions.includes('不会整组写入声纹库'), true)
  assert.equal(voiceReviewActions.includes('核对 {{ candidates.length }} 个样本'), true)
  assert.equal(voiceReviewActions.includes('试听最弱样本'), true)
  assert.equal(voiceReviewActions.includes('确认此样本'), true)
  assert.equal(voiceReviewActions.includes("!heard(candidate)"), true)
  assert.equal(voiceReviewActions.includes('mediaState.playing'), true)
  assert.equal(voiceReviewActions.includes('markHeard(candidate.prototype_id)'), true)
  assert.equal(voiceReviewActions.includes('expandedVoiceReviewIds.add(props.reviewId)'), true)
  assert.equal(voiceReviewActions.includes('expandedVoiceReviewIds.delete(props.reviewId)'), true)
  assert.equal(voiceReviewActions.includes("emit('review', candidate, 'uncertain')"), true)
  assert.equal(voiceReviewActions.includes("emit('review', candidate, 'rejected')"), true)
  assert.equal(voiceReviewActions.includes("emit('review', candidate, 'confirmed')"), true)
  assert.equal(reviews.includes('confirmReminder(item)'), true)
  assert.equal(reviews.includes('confirmMemory(item)'), true)
  assert.equal(reviewPolicy.includes("knowledge_proposal: '记忆确认'"), true)
  assert.equal(reviewPolicy.includes('只有逐个试听并确认的声音样本'), true)
  assert.equal(reviews.includes('acceptKnowledgeProposal'), true)
  assert.equal(reviews.includes('reviewDecisionReason(item)'), true)
  assert.equal(reviews.includes('帮助提升声音识别'), false)
  assert.equal(reviews.includes('voiceCandidateLedger'), false)
  assert.equal(people.includes('可选训练'), true)
  assert.equal(reviews.includes("if (item.kind === 'workflow_failure') continue"), true)
})

for (const { name, callback } of cases) {
  callback()
  console.log(`ok - ${name}`)
}

console.log(`${cases.length} frontend unit tests passed`)

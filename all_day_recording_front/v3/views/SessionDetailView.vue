<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import SegmentClassification from '../components/SegmentClassification.vue'
import { soundKindLabel } from '../core/soundKinds'
import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { copyText } from '../core/clipboard'
import {
  artifactKindLabel,
  formatBytes,
  formatDate,
  formatDuration,
  formatOffset,
  formatTimestamp,
  formatTimezone,
  producerLabel,
  shortSessionId,
  stageLabel,
  statusLabel,
} from '../core/format'
import { mediaState, playRange, playSessionRange, release, seek, stop } from '../core/media'
import { invalidateQuery, useQuery } from '../core/query'
import { navigate, route } from '../core/router'
import type { SegmentSummary, SelfIdentity, SpeakerTrackSummary, TimelinePage, Utterance } from '../core/types'
const sessionId = route.value.sessionId ?? ''
const tabs = [
  ['overview', '概览'], ['timeline', '时间线'], ['audio', '原音'], ['speakers', '说话人'], ['evidence', '证据'], ['runs', '运行历史'],
]
const activeTab = computed(() => tabs.some(([key]) => key === route.value.tab) ? route.value.tab ?? 'overview' : 'overview')
const timelineTab = computed(() => activeTab.value === 'timeline' || activeTab.value === 'speakers')
const audioTab = computed(() => activeTab.value === 'audio')
const overviewQuery = useQuery(`session-overview:${sessionId}`, (signal) => desktopApi.session(sessionId, 'overview', signal), () => !timelineTab.value && !audioTab.value)
const briefQuery = useQuery(`session-brief:${sessionId}`, (signal) => desktopApi.session(sessionId, 'brief', signal), () => audioTab.value)
const timelineSummaryQuery = useQuery(`session-timeline:${sessionId}`, (signal) => desktopApi.session(sessionId, 'timeline', signal), () => timelineTab.value)
const query = {
  data: computed(() => timelineTab.value ? timelineSummaryQuery.data.value : audioTab.value ? briefQuery.data.value : overviewQuery.data.value),
  loading: computed(() => timelineTab.value ? timelineSummaryQuery.loading.value : audioTab.value ? briefQuery.loading.value : overviewQuery.loading.value),
  error: computed(() => timelineTab.value ? timelineSummaryQuery.error.value : audioTab.value ? briefQuery.error.value : overviewQuery.error.value),
  refresh: async (force = false): Promise<void> => {
    await (timelineTab.value ? timelineSummaryQuery.refresh(force) : audioTab.value ? briefQuery.refresh(force) : overviewQuery.refresh(force))
  },
}
const classifying = ref<Utterance[] | null>(null)
const editing = ref<Utterance | null>(null)
const editSpeakerTracks = ref<Pick<SpeakerTrackSummary, 'speaker_track_id' | 'label' | 'person_name'>[] | null>(null)
const draft = ref('')
const draftSpeakerTrackId = ref('')
const draftIdentity = ref<SelfIdentity>('unknown')
const saving = ref(false)
const saveError = ref('')
const timelineSearch = ref('')
const timelineSpeaker = ref('all')
const timelineBucket = ref('all')
const timelineSegment = ref<SegmentSummary | null>(null)
const timelineItems = ref<Utterance[]>([])
const timelineMeta = ref<TimelinePage | null>(null)
const speakerSummary = ref<TimelinePage['speakers'] | null>(null)
let speakerRequest: Promise<void> | null = null
const timelineCursor = ref<string | null>(null)
const timelineLoading = ref(false)
const timelineError = ref('')
const batchLoading = ref(false)
let timelineRequest = 0
let timelineAbort: AbortController | null = null
let timelineTimer: number | undefined
const timelineDensity = ref<'compact' | 'comfortable'>('compact')
const expandedTurns = ref<Set<string>>(new Set())
const playingKey = ref('')
type ActivePlayback = {
  key: string
  label: string
  startMs: number
  endMs: number
} & ({
  kind: 'session'
  sessionStartMs: number
  sessionEndMs: number
} | {
  kind: 'media'
  mediaId: string
})
const activePlayback = ref<ActivePlayback | null>(null)
const notice = ref('')
let noticeTimer: number | undefined
interface TranscriptTurn {
  key: string
  items: Utterance[]
  text: string
  speaker: string
  identity: SelfIdentity
  startAt: string
  startMs: number
  endMs: number
}
const speakerTracks = computed(() => new Map(
  (timelineMeta.value?.speaker_tracks ?? query.data.value?.speaker_tracks ?? []).map((item) => [item.speaker_track_id, item]),
))
const speakers = computed(() => {
  if (speakerSummary.value) return speakerSummary.value.map((item) => ({
    label: item.label === 'unassigned' ? '未分配' : item.label,
    count: item.count, trackId: item.speaker_track_id, personName: item.person_name,
  }))
  const values = new Map<string, { count: number; trackId: string | null; personName: string | null }>()
  for (const item of query.data.value?.utterances ?? []) {
    const label = item.speaker_label ?? '未分配'
    const current = values.get(label)
    const track = item.speaker_track_id ? speakerTracks.value.get(item.speaker_track_id) : undefined
    values.set(label, {
      count: (current?.count ?? 0) + 1,
      trackId: item.speaker_track_id,
      personName: current?.personName ?? track?.person_name ?? null,
    })
  }
  return [...values.entries()].map(([label, value]) => ({ label, ...value }))
})
const speakerOptions = computed(() => speakers.value.map((item) => ({
  value: item.label === '未分配' ? 'unassigned' : item.label,
  label: item.personName ?? friendlySpeakerLabel(item.label),
})))
const timelineRanges = computed(() => {
  const duration = query.data.value?.session.duration_ms ?? 0
  return Array.from({ length: Math.ceil(duration / 600_000) }, (_, index) => {
    const start = index * 600_000
    return {
      value: String(start),
      label: `${formatOffset(start).slice(0, 5)}–${formatOffset(Math.min(start + 600_000, duration)).slice(0, 5)}`,
    }
  })
})
const filteredTimeline = computed(() => buildTranscriptTurns(timelineItems.value))
const visibleTimeline = filteredTimeline
const hasMoreTimeline = computed(() => timelineCursor.value !== null)
const filteredUtteranceCount = computed(() => timelineItems.value.length)
const playbackElapsedMs = computed(() => activePlayback.value
  ? Math.max(0, mediaState.currentTimeMs - activePlayback.value.startMs)
  : 0)
const playbackDurationMs = computed(() => activePlayback.value
  ? Math.max(0, activePlayback.value.endMs - activePlayback.value.startMs)
  : 0)
async function loadTimeline(append = false): Promise<void> {
  if (append && !timelineCursor.value) return
  const requestId = ++timelineRequest
  timelineAbort?.abort()
  const controller = new AbortController()
  timelineAbort = controller
  timelineLoading.value = true
  timelineError.value = ''
  const bucketStart = timelineBucket.value === 'all' ? undefined : Number(timelineBucket.value)
  try {
    const page = await desktopApi.timeline(sessionId, {
      cursor: append ? timelineCursor.value : null,
      q: timelineSearch.value.trim(), speaker: timelineSpeaker.value === 'unassigned' ? 'unassigned' : timelineSpeaker.value,
      startMs: timelineSegment.value?.session_start_ms ?? bucketStart,
      endMs: timelineSegment.value?.session_end_ms ?? (bucketStart === undefined ? undefined : bucketStart + 600_000),
    }, controller.signal)
    if (requestId !== timelineRequest) return
    timelineItems.value = append ? [...timelineItems.value, ...page.items] : page.items
    timelineCursor.value = page.next_cursor
    timelineMeta.value = page
  } catch (error) {
    if (requestId !== timelineRequest || controller.signal.aborted) return
    timelineError.value = error instanceof Error ? error.message : String(error)
  } finally {
    if (requestId === timelineRequest) timelineLoading.value = false
  }
}
function loadSpeakers(): Promise<void> {
  if (speakerSummary.value !== null) return Promise.resolve()
  if (speakerRequest) return speakerRequest
  speakerRequest = desktopApi.timelineSpeakers(sessionId)
    .then((page) => { speakerSummary.value = page.items })
    .catch((error) => { showNotice(error instanceof Error ? error.message : String(error)) })
    .finally(() => { speakerRequest = null })
  return speakerRequest
}
watch([activeTab, timelineSearch, timelineSpeaker, timelineBucket, timelineSegment], () => {
  window.clearTimeout(timelineTimer)
  timelineRequest += 1
  timelineAbort?.abort()
  timelineCursor.value = null
  timelineLoading.value = false
  if (activeTab.value === 'timeline') {
    timelineTimer = window.setTimeout(() => void loadTimeline(), timelineSearch.value ? 220 : 0)
  }
  if (activeTab.value === 'speakers') void loadSpeakers()
}, { immediate: true })
onBeforeUnmount(() => {
  window.clearTimeout(noticeTimer)
  window.clearTimeout(timelineTimer)
  timelineAbort?.abort()
  release()
})
function showNotice(message: string): void {
  notice.value = message
  window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => { notice.value = '' }, 2600)
}
function selectTab(tab: string): void {
  stopPlayback()
  if (tab === 'timeline') timelineSegment.value = null
  navigate(`/recordings/${encodeURIComponent(sessionId)}?tab=${tab}`)
}
function friendlySpeakerLabel(label: string): string {
  if (label === '未分配') return '未分配说话人'
  return `说话人 ${label.replace(/^SPEAKER_/, '')}`
}
function displaySpeaker(item: Utterance): string {
  const track = item.speaker_track_id ? speakerTracks.value.get(item.speaker_track_id) : undefined
  return item.person_name ?? track?.person_name ?? friendlySpeakerLabel(item.speaker_label ?? '未分配')
}
async function classifyAll(): Promise<void> {
  batchLoading.value = true
  try {
    const detail = await desktopApi.session(sessionId, 'full')
    classifying.value = detail.utterances
  } catch (error) {
    showNotice(error instanceof Error ? error.message : String(error))
  } finally {
    batchLoading.value = false
  }
}
function joinTranscriptText(left: string, right: string): string {
  if (!left) return right
  if (!right) return left
  const noLeadingSpace = /^[,.;!?，。！？；：、…）】》”’]/u.test(right)
  const chineseJoin = /[\p{Script=Han}，。！？；：、…）】》”’]$/u.test(left)
    && /^[\p{Script=Han}，。！？；：、…（【《“‘]/u.test(right)
  return `${left}${noLeadingSpace || chineseJoin ? '' : ' '}${right}`
}
function buildTranscriptTurns(items: Utterance[]): TranscriptTurn[] {
  const turns: TranscriptTurn[] = []
  const ordered = [...items].sort((left, right) => left.start_ms - right.start_ms || left.utterance_id.localeCompare(right.utterance_id))
  for (const item of ordered) {
    const previousTurn = turns.at(-1)
    const previousItem = previousTurn?.items.at(-1)
    const sameSpeaker = previousItem !== undefined
      && (previousItem.speaker_track_id ?? previousItem.speaker_label ?? 'unassigned')
        === (item.speaker_track_id ?? item.speaker_label ?? 'unassigned')
    const closeEnough = previousItem !== undefined && item.start_ms - previousItem.end_ms <= 3_000
    const withinTurnLength = previousTurn !== undefined && item.end_ms - previousTurn.startMs <= 30_000
    const withinTextLength = previousTurn !== undefined && previousTurn.text.length + item.text.length <= 160
    const correctionBoundary = previousItem !== undefined && (previousItem.revision > 1 || item.revision > 1)
    if (previousTurn && previousItem && sameSpeaker && closeEnough && withinTurnLength && withinTextLength
      && previousItem.identity === item.identity && !correctionBoundary) {
      previousTurn.items.push(item)
      previousTurn.text = joinTranscriptText(previousTurn.text, item.text)
      previousTurn.endMs = Math.max(previousTurn.endMs, item.end_ms)
      continue
    }
    turns.push({
      key: item.utterance_id,
      items: [item],
      text: item.text,
      speaker: displaySpeaker(item),
      identity: item.identity,
      startAt: item.start_at,
      startMs: item.start_ms,
      endMs: item.end_ms,
    })
  }
  return turns
}
function isTurnExpanded(turn: TranscriptTurn): boolean {
  return expandedTurns.value.has(turn.key)
}
function toggleTurnDetails(turn: TranscriptTurn): void {
  const next = new Set(expandedTurns.value)
  if (next.has(turn.key)) next.delete(turn.key)
  else next.add(turn.key)
  expandedTurns.value = next
}
async function edit(item: Utterance): Promise<void> {
  try {
    const [fresh, tracks] = await Promise.all([
      desktopApi.timelineUtterance(sessionId, item.utterance_id),
      editSpeakerTracks.value === null ? desktopApi.timelineSpeakerTracks(sessionId).then((value) => value.items) : Promise.resolve(editSpeakerTracks.value),
    ])
    editSpeakerTracks.value = tracks
    editing.value = fresh
    draft.value = fresh.text
    draftSpeakerTrackId.value = fresh.speaker_track_id ?? ''
    draftIdentity.value = fresh.identity
    saveError.value = ''
  } catch (error) { showNotice(error instanceof Error ? error.message : String(error)) }
}
function hasCorrectionChanges(): boolean {
  return editing.value !== null && (
    draft.value.trim() !== editing.value.text
    || (draftSpeakerTrackId.value || null) !== editing.value.speaker_track_id
    || draftIdentity.value !== editing.value.identity
  )
}
function identityLabel(identity: SelfIdentity): string {
  return identity === 'self' ? '本人' : identity === 'not_self' ? '非本人' : '未知'
}
function playbackSessionEnd(startMs: number, endMs: number): number {
  return Math.min(
    query.data.value?.session.duration_ms ?? endMs,
    Math.max(endMs, startMs + 1_000),
  )
}
function sessionRangeAvailable(startMs: number, endMs: number): boolean {
  let cursor = startMs
  const segments = [...(query.data.value?.segment_ranges ?? query.data.value?.segments ?? [])]
    .sort((left, right) => left.session_start_ms - right.session_start_ms)
  for (const segment of segments) {
    if (segment.session_end_ms <= cursor) continue
    if (segment.session_start_ms > cursor) return false
    cursor = Math.min(endMs, Math.max(cursor, segment.session_end_ms))
    if (cursor >= endMs) return true
  }
  return false
}
function stopPlayback(): void {
  stop()
  playingKey.value = ''
  activePlayback.value = null
}
function toggleUtterance(item: Utterance): void {
  const key = `utterance:${item.utterance_id}`
  if (mediaState.playing && playingKey.value === key) {
    stopPlayback()
    return
  }
  const sessionEnd = playbackSessionEnd(item.start_ms, item.end_ms)
  if (!sessionRangeAvailable(item.start_ms, sessionEnd)) {
    showNotice('这条转写没有连续可用的原音')
    return
  }
  playingKey.value = key
  activePlayback.value = {
    kind: 'session', key, label: `${displaySpeaker(item)} · ${formatOffset(item.start_ms)}`,
    sessionStartMs: item.start_ms, sessionEndMs: sessionEnd,
    startMs: 0, endMs: sessionEnd - item.start_ms,
  }
  playSessionRange(sessionId, item.start_ms, sessionEnd)
}
function toggleTurn(turn: TranscriptTurn): void {
  const key = `turn:${turn.key}`
  if (mediaState.playing && playingKey.value === key) {
    stopPlayback()
    return
  }
  const sessionEnd = playbackSessionEnd(turn.startMs, turn.endMs)
  if (!sessionRangeAvailable(turn.startMs, sessionEnd)) {
    showNotice('这段发言没有连续可用的原音')
    return
  }
  playingKey.value = key
  activePlayback.value = {
    kind: 'session', key, label: `${turn.speaker} · ${formatOffset(turn.startMs)}`,
    sessionStartMs: turn.startMs, sessionEndMs: sessionEnd,
    startMs: 0, endMs: sessionEnd - turn.startMs,
  }
  playSessionRange(sessionId, turn.startMs, sessionEnd)
}
function toggleSegment(item: SegmentSummary): void {
  const key = `segment:${item.segment_id}`
  if (mediaState.playing && playingKey.value === key) {
    stopPlayback()
    return
  }
  playingKey.value = key
  activePlayback.value = { kind: 'media', key, label: `原音分片 #${String(item.sequence).padStart(3, '0')}`, mediaId: item.media_id, startMs: item.source_start_ms, endMs: item.source_end_ms }
  playRange(item.media_id, item.source_start_ms, item.source_end_ms)
}
function toggleActivePlayback(): void {
  if (!activePlayback.value) return
  if (mediaState.playing) {
    stop()
    return
  }
  playingKey.value = activePlayback.value.key
  const current = mediaState.currentTimeMs
  const resumeAt = current < activePlayback.value.startMs || current >= activePlayback.value.endMs - 100
    ? activePlayback.value.startMs
    : current
  if (activePlayback.value.kind === 'session') {
    playSessionRange(
      sessionId,
      activePlayback.value.sessionStartMs,
      activePlayback.value.sessionEndMs,
      resumeAt,
    )
  } else {
    playRange(activePlayback.value.mediaId, resumeAt, activePlayback.value.endMs)
  }
}
function seekPlayback(event: Event): void {
  seek(Number((event.target as HTMLInputElement).value))
}
function showSegmentInTimeline(item: SegmentSummary): void {
  stopPlayback()
  timelineSearch.value = ''
  timelineSpeaker.value = 'all'
  timelineBucket.value = 'all'
  timelineSegment.value = item
  navigate(`/recordings/${encodeURIComponent(sessionId)}?tab=timeline`)
}

function clearTimelineFilters(): void {
  timelineSearch.value = ''
  timelineSpeaker.value = 'all'
  timelineBucket.value = 'all'
  timelineSegment.value = null
}

async function copyValue(value: string, label: string): Promise<void> {
  showNotice(await copyText(value) ? `${label}已复制` : `${label}复制失败`)
}

async function saveCorrection(): Promise<void> {
  if (!editing.value || !draft.value.trim() || !hasCorrectionChanges()) return
  if (!window.confirm('保存会创建新的校正版本，并只将依赖这条转写的结果标记为待更新。继续吗？')) return
  saving.value = true
  saveError.value = ''
  try {
    await desktopApi.correctUtterance(
      editing.value.utterance_id,
      editing.value.revision,
      draft.value.trim(),
      draftSpeakerTrackId.value || null,
      draftIdentity.value,
    )
    for (const key of ['persons', 'person-choices', 'people-voice', 'reviews', 'daily-summaries', 'relationship-reports']) invalidateQuery(key)
    editing.value = null
    await query.refresh(true)
    await loadTimeline()
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : String(error)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="page detail-page">
    <button class="back-link" @click="navigate('/recordings')">← 返回录音库</button>
    <PageState :loading="query.loading.value" :error="query.error.value" :has-content="query.data.value !== null" @retry="query.refresh(true)">
      <template v-if="query.data.value">
        <header class="detail-hero">
          <div>
            <div class="session-id-line">
              <p class="overline">录音标识 / {{ shortSessionId(sessionId) }}</p>
              <button class="mini-action" type="button" @click="copyValue(sessionId, 'Session ID')">复制完整 ID</button>
            </div>
            <h1>{{ formatDate(query.data.value.session.captured_start) }}</h1>
            <p>{{ formatDuration(query.data.value.session.duration_ms) }} · {{ formatTimezone(query.data.value.session.timezone) }} · 本地版本 {{ query.data.value.session.revision }}</p>
          </div>
          <span class="large-status" :data-status="query.data.value.session.status_code"><i></i>{{ statusLabel(query.data.value.session.processing_status ?? query.data.value.session.status_code) }}</span>
        </header>
        <p v-if="notice" class="page-notice" aria-live="polite">{{ notice }}</p>
        <nav class="detail-tabs" aria-label="会话详情栏目">
          <button
            v-for="tab in tabs"
            :key="tab[0]"
            :class="{ active: activeTab === tab[0] }"
            :aria-current="activeTab === tab[0] ? 'page' : undefined"
            @click="selectTab(tab[0])"
          >{{ tab[1] }}</button>
        </nav>

        <div v-if="activeTab === 'overview'" class="detail-grid">
          <section class="panel detail-card"><p class="section-kicker">原音完整性</p><h2>原音与准入</h2><dl><div><dt>连续分片</dt><dd>{{ query.data.value.session.segment_count }}</dd></div><div><dt>电脑副本</dt><dd>{{ query.data.value.segments.every((item) => item.replica_state === 'available') ? '全部可用' : '需要检查' }}</dd></div><div><dt>独立备份</dt><dd>{{ query.data.value.backups.some((item) => item.status === 'verified') ? '恢复演练通过' : '尚未准入' }}</dd></div></dl></section>
          <section class="panel detail-card"><p class="section-kicker">处理进度</p><h2>当前处理</h2><dl><div><dt>当前阶段</dt><dd>{{ stageLabel(query.data.value.session.current_stage) }}</dd></div><div><dt>整体进度</dt><dd>{{ Math.round(query.data.value.session.progress * 100) }}%</dd></div><div><dt>处理版本</dt><dd>{{ query.data.value.runs[0]?.pipeline_version ?? '尚未运行' }}</dd></div></dl></section>
          <section class="panel detail-card"><p class="section-kicker">可追溯结果</p><h2>结果与证据</h2><dl><div><dt>转写片段</dt><dd>{{ query.data.value.utterance_count }}</dd></div><div><dt>证据制品</dt><dd>{{ query.data.value.artifacts.length }}</dd></div><div><dt>待重新生成</dt><dd>{{ query.data.value.artifacts.filter((item) => item.status === 'stale').length }}</dd></div></dl></section>
        </div>

        <section v-else-if="activeTab === 'timeline'" class="panel timeline-panel">
          <header class="timeline-heading"><div><p class="section-kicker">转写时间线</p><h2>对话时间线</h2></div><span>已加载 {{ filteredUtteranceCount }} / {{ timelineMeta?.total ?? query.data.value.utterance_count }} 条转写</span></header>
          <button class="quiet-button" :disabled="batchLoading" @click="classifyAll">{{ batchLoading ? '正在读取完整会话…' : '批量标注声音类型' }}</button>
          <div class="timeline-controls">
            <label><span>搜索转写</span><input v-model="timelineSearch" type="search" placeholder="输入关键词或时间" /></label>
            <label><span>说话人</span><select v-model="timelineSpeaker" @focus="loadSpeakers" @pointerdown="loadSpeakers"><option value="all">全部说话人</option><option v-if="speakerSummary === null" disabled value="loading">正在读取说话人…</option><option v-for="item in speakerOptions" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
            <label><span>时间范围</span><select v-model="timelineBucket" @change="timelineSegment = null"><option value="all">全部时间</option><option v-for="item in timelineRanges" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
            <div class="density-toggle" aria-label="时间线显示密度">
              <button type="button" :class="{ active: timelineDensity === 'compact' }" :aria-pressed="timelineDensity === 'compact'" @click="timelineDensity = 'compact'">紧凑</button>
              <button type="button" :class="{ active: timelineDensity === 'comfortable' }" :aria-pressed="timelineDensity === 'comfortable'" @click="timelineDensity = 'comfortable'">舒适</button>
            </div>
            <button v-if="timelineSearch || timelineSpeaker !== 'all' || timelineBucket !== 'all' || timelineSegment" class="quiet-button" type="button" @click="clearTimelineFilters">清除筛选</button>
          </div>
          <p v-if="timelineSegment" class="timeline-focus-note">正在查看分片 #{{ String(timelineSegment.sequence).padStart(3, '0') }} · {{ formatOffset(timelineSegment.session_start_ms) }}–{{ formatOffset(timelineSegment.session_end_ms) }}</p>
          <div v-if="timelineLoading && !timelineItems.length" class="inline-empty">正在读取转写…</div>
          <div v-else-if="timelineError" class="inline-empty">{{ timelineError }} <button class="text-button" @click="loadTimeline()">重试</button></div>
          <div v-else-if="!filteredTimeline.length" class="inline-empty">没有匹配的发言段。</div>
          <div class="transcript-list" :data-density="timelineDensity">
            <article v-for="turn in visibleTimeline" :key="turn.key" class="turn-row" :class="{ playing: mediaState.playing && playingKey === `turn:${turn.key}`, expanded: isTurnExpanded(turn) }">
              <button class="turn-time" type="button" :disabled="!sessionRangeAvailable(turn.startMs, playbackSessionEnd(turn.startMs, turn.endMs))" :title="sessionRangeAvailable(turn.startMs, playbackSessionEnd(turn.startMs, turn.endMs)) ? `播放 ${formatTimestamp(turn.startAt)} 的连续原音` : '没有连续可用的原音'" :aria-label="`播放 ${turn.speaker} 在 ${formatOffset(turn.startMs)} 的发言`" @click="toggleTurn(turn)">
                <span>{{ formatOffset(turn.startMs) }}</span><small>{{ formatTimestamp(turn.startAt) }}</small>
              </button>
              <div class="turn-speaker"><strong>{{ turn.speaker }}</strong><span class="identity-chip" :data-identity="turn.identity">{{ identityLabel(turn.identity) }}</span><small v-if="turn.items.length > 1">{{ turn.items.length }} 个片段</small></div>
              <div class="turn-copy"><p>{{ turn.text }}</p><small>{{ [...new Set(turn.items.map(soundKindLabel))].join('、') }}</small><small v-if="turn.items.some((item) => item.revision > 1)" class="corrected-note">含人工校正</small></div>
              <div class="turn-actions"><button class="text-button" @click="classifying = turn.items">声音分类</button>
                <button v-if="turn.items.length === 1" class="text-button" type="button" @click="edit(turn.items[0])">校正</button>
                <button v-else class="text-button" type="button" :aria-expanded="isTurnExpanded(turn)" @click="toggleTurnDetails(turn)">{{ isTurnExpanded(turn) ? '收起' : '逐句校正' }}</button>
              </div>
              <div v-if="isTurnExpanded(turn)" class="turn-details">
                <div v-for="item in turn.items" :key="item.utterance_id" class="turn-fragment">
                  <button type="button" :disabled="!sessionRangeAvailable(item.start_ms, playbackSessionEnd(item.start_ms, item.end_ms))" :aria-label="`播放 ${formatOffset(item.start_ms)} 的原音`" @click="toggleUtterance(item)">{{ formatOffset(item.start_ms) }}</button>
                  <p>{{ item.text }} <small>{{ soundKindLabel(item) }}</small></p>
                  <small>版本 {{ item.revision }} · {{ formatOffset(item.end_ms - item.start_ms) }}</small>
                  <button class="text-button" type="button" @click="edit(item)">校正</button><button class="text-button" @click="classifying = [item]">声音分类</button>
                </div>
              </div>
            </article>
          </div>
          <footer v-if="hasMoreTimeline" class="load-more-row"><button class="quiet-button" type="button" :disabled="timelineLoading" @click="loadTimeline(true)">{{ timelineLoading ? '读取中…' : '继续加载转写' }}</button></footer>
          <aside v-if="activePlayback" class="timeline-player" aria-label="录音播放器">
            <button type="button" class="player-toggle" :aria-label="mediaState.playing ? '暂停播放' : '继续播放'" @click="toggleActivePlayback">{{ mediaState.playing ? 'Ⅱ' : '▶' }}</button>
            <div class="player-copy"><strong>{{ activePlayback.label }}</strong><small>{{ formatOffset(playbackElapsedMs) }} / {{ formatOffset(playbackDurationMs) }}</small></div>
            <input :value="mediaState.currentTimeMs" type="range" :min="activePlayback.startMs" :max="activePlayback.endMs" step="100" aria-label="播放进度" @input="seekPlayback" />
            <button type="button" class="player-close" aria-label="关闭播放器" @click="stopPlayback">×</button>
          </aside>
        </section>

        <section v-else-if="activeTab === 'audio'" class="panel list-panel">
          <header><div><p class="section-kicker">不可变原音</p><h2>原音分片与副本</h2></div><span>{{ query.data.value.segments.length }} 个连续分片</span></header>
          <article v-for="item in query.data.value.segments" :key="item.segment_id" class="data-row audio-data-row">
            <span><strong>#{{ String(item.sequence).padStart(3, '0') }}</strong><small>{{ formatOffset(item.session_start_ms) }}–{{ formatOffset(item.session_end_ms) }}</small></span>
            <span><strong>{{ item.device_name ?? '未知设备' }}</strong><small>{{ item.format.toUpperCase() }} · {{ formatBytes(item.size_bytes) }}</small></span>
            <code :title="item.sha256">{{ item.sha256.slice(0, 16) }}…</code>
            <span class="status-pill">{{ statusLabel(item.replica_state) }}</span>
            <span class="row-actions">
              <button class="mini-action" type="button" @click="toggleSegment(item)">{{ mediaState.playing && playingKey === `segment:${item.segment_id}` ? '停止' : '播放' }}</button>
              <button class="mini-action" type="button" @click="showSegmentInTimeline(item)">看时间线</button>
              <button class="mini-action" type="button" @click="copyValue(item.sha256, '原音摘要')">复制摘要</button>
            </span>
          </article>
        </section>

        <section v-else-if="activeTab === 'speakers'" class="speaker-section">
          <header class="panel-flat speaker-section-header"><div><p class="section-kicker">说话人轨道</p><h2>本段录音中的说话人</h2></div><span>人物姓名优先；尚未确认时显示轨道编号</span></header>
          <div class="speaker-grid">
            <article v-for="speaker in speakers" :key="speaker.label" class="panel speaker-card"><span class="speaker-mark">{{ speaker.label === '未分配' ? '?' : speaker.label.slice(-2) }}</span><div><strong>{{ speaker.personName ?? friendlySpeakerLabel(speaker.label) }}</strong><small>{{ speaker.count }} 条转写<template v-if="speaker.personName"> · 轨道 {{ speaker.label }}</template></small></div></article>
          </div>
        </section>

        <section v-else-if="activeTab === 'evidence'" class="panel list-panel">
          <header><div><p class="section-kicker">不可变证据制品</p><h2>证据与依赖状态</h2></div></header>
          <article v-for="item in query.data.value.artifacts" :key="item.artifact_id" class="data-row artifact-data-row">
            <span><strong>{{ artifactKindLabel(item.kind) }}</strong><small>{{ producerLabel(item.producer) }} · {{ item.producer_version }}</small></span>
            <span><strong>{{ formatBytes(item.size_bytes) }}</strong><small>{{ formatDate(item.created_at) }}</small></span>
            <code :title="`${item.kind} · ${item.sha256 ?? ''}`">{{ item.sha256 ? `${item.sha256.slice(0, 16)}…` : '摘要不可用' }}</code>
            <span class="status-pill" :data-status="item.status">{{ statusLabel(item.status) }}</span>
            <button v-if="item.sha256" class="mini-action" type="button" @click="copyValue(item.sha256, '证据摘要')">复制摘要</button>
          </article>
        </section>

        <section v-else-if="activeTab === 'runs'" class="panel list-panel">
          <header><div><p class="section-kicker">处理记录</p><h2>持久运行历史</h2></div></header>
          <button v-for="item in query.data.value.runs" :key="item.run_id" class="data-row button-row" @click="item.job_id && navigate(`/processing?job=${encodeURIComponent(item.job_id)}`)"><span><strong>{{ item.pipeline_version }}</strong><small>{{ formatDate(item.created_at) }} · 版本 {{ item.revision }}</small></span><span><strong>{{ stageLabel(item.current_stage) }}</strong><small>{{ Math.round(item.progress * 100) }}%</small></span><span class="status-pill">{{ statusLabel(item.status) }}</span><b>→</b></button>
        </section>
      </template>
    </PageState>

    <SegmentClassification v-if="classifying" :items="classifying" @close="classifying = null" @saved="classifying = null; query.refresh(true); loadTimeline()" />
    <div v-if="editing" class="dialog-backdrop" @click.self="editing = null">
      <form class="dialog" @submit.prevent="saveCorrection">
        <p class="section-kicker">人工校正</p><h2>校正转写片段</h2><p>原始证据不会被覆盖；保存后创建版本 {{ editing.revision + 1 }}。</p>
        <p class="correction-baseline"><strong>模型原始结果 · {{ editing.original_speaker_label ? friendlySpeakerLabel(editing.original_speaker_label) : '未分配说话人' }} · {{ identityLabel(editing.original_identity) }}</strong>{{ editing.original_text }}</p>
        <label><span>说话人轨道</span><select v-model="draftSpeakerTrackId"><option value="">未分配 / 未知</option><option v-for="track in editSpeakerTracks ?? []" :key="track.speaker_track_id" :value="track.speaker_track_id">{{ track.person_name ?? friendlySpeakerLabel(track.label) }}</option></select></label>
        <label><span>本人身份</span><select v-model="draftIdentity"><option value="self">本人</option><option value="not_self">非本人</option><option value="unknown">未知 / 证据不足</option></select></label>
        <label><span>转写文本</span><textarea v-model="draft" rows="5" aria-label="校正文本"></textarea></label>
        <p v-if="saveError" class="form-error">{{ saveError }}</p>
        <div><button type="button" class="quiet-button" @click="editing = null">取消</button><button class="primary-action" :disabled="saving || !draft.trim() || !hasCorrectionChanges()">{{ saving ? '保存中' : '保存新版本' }}</button></div>
      </form>
    </div>
  </section>
</template>

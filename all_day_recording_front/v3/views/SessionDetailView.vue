<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatBytes, formatDate, formatDuration, formatOffset, formatTimestamp, statusLabel } from '../core/format'
import { playRange, release, stop } from '../core/media'
import { useQuery } from '../core/query'
import { navigate, route } from '../core/router'
import type { SegmentSummary, SelfIdentity, Utterance } from '../core/types'

const sessionId = route.value.sessionId ?? ''
const query = useQuery(`session:${sessionId}`, () => desktopApi.session(sessionId))
const tabs = [
  ['overview', '概览'], ['timeline', '时间线'], ['audio', '原音'], ['speakers', '说话人'], ['evidence', '证据'], ['runs', '运行历史'],
]
const activeTab = computed(() => tabs.some(([key]) => key === route.value.tab) ? route.value.tab ?? 'overview' : 'overview')
const editing = ref<Utterance | null>(null)
const draft = ref('')
const draftSpeakerTrackId = ref('')
const draftIdentity = ref<SelfIdentity>('unknown')
const saving = ref(false)
const saveError = ref('')
const speakers = computed(() => {
  const values = new Map<string, number>()
  for (const item of query.data.value?.utterances ?? []) {
    const label = item.speaker_label ?? '未分配'
    values.set(label, (values.get(label) ?? 0) + 1)
  }
  return [...values.entries()]
})

onBeforeUnmount(release)

function selectTab(tab: string): void {
  stop()
  navigate(`/recordings/${encodeURIComponent(sessionId)}?tab=${tab}`)
}

function edit(item: Utterance): void {
  editing.value = item
  draft.value = item.text
  draftSpeakerTrackId.value = item.speaker_track_id ?? ''
  draftIdentity.value = item.identity
  saveError.value = ''
}

function hasCorrectionChanges(): boolean {
  return editing.value !== null && (
    draft.value.trim() !== editing.value.text ||
    (draftSpeakerTrackId.value || null) !== editing.value.speaker_track_id ||
    draftIdentity.value !== editing.value.identity
  )
}

function identityLabel(identity: SelfIdentity): string {
  return identity === 'self' ? '本人' : identity === 'not_self' ? '非本人' : '未知'
}

function utteranceSegment(item: Utterance): SegmentSummary | undefined {
  return query.data.value?.segments.find((candidate) =>
    candidate.session_start_ms <= item.start_ms && candidate.session_end_ms >= item.end_ms)
}

function playUtterance(item: Utterance): void {
  const segment = utteranceSegment(item)
  if (!segment) return
  const start = segment.source_start_ms + item.start_ms - segment.session_start_ms
  playRange(segment.media_id, start, start + item.end_ms - item.start_ms)
}

async function saveCorrection(): Promise<void> {
  if (!editing.value || !draft.value.trim() || !hasCorrectionChanges()) return
  if (!window.confirm('保存会新增 correction revision，并只将依赖该 utterance 的制品标记为 stale。继续吗？')) return
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
    editing.value = null
    await query.refresh(true)
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
    <PageState :loading="query.loading.value" :error="query.error.value" @retry="query.refresh(true)">
      <template v-if="query.data.value">
        <header class="detail-hero">
          <div><p class="overline">SESSION / {{ sessionId.slice(-10) }}</p><h1>{{ formatDate(query.data.value.session.captured_start) }}</h1><p>{{ formatDuration(query.data.value.session.duration_ms) }} · {{ query.data.value.session.timezone }} · revision {{ query.data.value.session.revision }}</p></div>
          <span class="large-status" :data-status="query.data.value.session.status_code"><i></i>{{ statusLabel(query.data.value.session.processing_status ?? query.data.value.session.status_code) }}</span>
        </header>
        <nav class="detail-tabs" aria-label="会话详情栏目">
          <button v-for="tab in tabs" :key="tab[0]" :class="{ active: activeTab === tab[0] }" @click="selectTab(tab[0])">{{ tab[1] }}</button>
        </nav>

        <div v-if="activeTab === 'overview'" class="detail-grid">
          <section class="panel detail-card"><p class="section-kicker">INTEGRITY</p><h2>原音与准入</h2><dl><div><dt>连续分片</dt><dd>{{ query.data.value.session.segment_count }}</dd></div><div><dt>电脑副本</dt><dd>{{ query.data.value.segments.every((item) => item.replica_state === 'available') ? '全部可用' : '需要检查' }}</dd></div><div><dt>独立备份</dt><dd>{{ query.data.value.backups.some((item) => item.status === 'verified') ? '恢复演练通过' : '尚未准入' }}</dd></div></dl></section>
          <section class="panel detail-card"><p class="section-kicker">PROCESSING</p><h2>当前处理</h2><dl><div><dt>阶段</dt><dd>{{ query.data.value.session.current_stage ?? '—' }}</dd></div><div><dt>进度</dt><dd>{{ Math.round(query.data.value.session.progress * 100) }}%</dd></div><div><dt>运行版本</dt><dd>{{ query.data.value.runs[0]?.pipeline_version ?? '尚未运行' }}</dd></div></dl></section>
          <section class="panel detail-card"><p class="section-kicker">EVIDENCE</p><h2>可追溯结果</h2><dl><div><dt>utterance</dt><dd>{{ query.data.value.utterances.length }}</dd></div><div><dt>artifact</dt><dd>{{ query.data.value.artifacts.length }}</dd></div><div><dt>stale</dt><dd>{{ query.data.value.artifacts.filter((item) => item.status === 'stale').length }}</dd></div></dl></section>
        </div>

        <section v-else-if="activeTab === 'timeline'" class="panel timeline-panel">
          <header><div><p class="section-kicker">UTTERANCE TIMELINE</p><h2>对话时间线</h2></div><span>{{ query.data.value.utterances.length }} 条</span></header>
          <article v-for="item in query.data.value.utterances" :key="item.utterance_id" class="utterance-row">
            <button class="play-dot" :disabled="!utteranceSegment(item)" aria-label="播放对应原音" @click="playUtterance(item)">▶</button>
            <span class="utterance-time">{{ formatTimestamp(item.start_at) }}</span>
            <div><strong>{{ item.speaker_label ?? '未分配说话人' }} <span class="identity-chip" :data-identity="item.identity">{{ identityLabel(item.identity) }}</span></strong><p>{{ item.text }}</p><small>{{ formatOffset(item.start_ms) }} · revision {{ item.revision }} · {{ formatOffset(item.end_ms - item.start_ms) }}<template v-if="item.revision > 1"> · 已人工校正</template><template v-if="item.identity !== item.original_identity"> · 身份原始值 {{ identityLabel(item.original_identity) }}</template></small></div>
            <button class="text-button" @click="edit(item)">校正</button>
          </article>
        </section>

        <section v-else-if="activeTab === 'audio'" class="panel list-panel">
          <header><div><p class="section-kicker">IMMUTABLE AUDIO</p><h2>原音分片与副本</h2></div></header>
          <article v-for="item in query.data.value.segments" :key="item.segment_id" class="data-row"><span><strong>#{{ String(item.sequence).padStart(3, '0') }}</strong><small>{{ formatOffset(item.session_start_ms) }}–{{ formatOffset(item.session_end_ms) }}</small></span><span><strong>{{ item.device_name ?? '未知设备' }}</strong><small>{{ item.format.toUpperCase() }} · {{ formatBytes(item.size_bytes) }}</small></span><code>{{ item.sha256.slice(0, 16) }}…</code><span class="status-pill">{{ statusLabel(item.replica_state) }}</span></article>
        </section>

        <section v-else-if="activeTab === 'speakers'" class="speaker-grid">
          <article v-for="speaker in speakers" :key="speaker[0]" class="panel speaker-card"><span class="speaker-mark">{{ speaker[0].slice(-2) }}</span><div><strong>{{ speaker[0] }}</strong><small>{{ speaker[1] }} 条 utterance · 当前仅显示 track 标签</small></div></article>
        </section>

        <section v-else-if="activeTab === 'evidence'" class="panel list-panel">
          <header><div><p class="section-kicker">IMMUTABLE ARTIFACTS</p><h2>证据与依赖状态</h2></div></header>
          <article v-for="item in query.data.value.artifacts" :key="item.artifact_id" class="data-row"><span><strong>{{ item.kind }}</strong><small>{{ item.producer }} / {{ item.producer_version }}</small></span><span><strong>{{ formatBytes(item.size_bytes) }}</strong><small>{{ formatDate(item.created_at) }}</small></span><code>{{ item.sha256 ? `${item.sha256.slice(0, 16)}…` : '摘要不可用' }}</code><span class="status-pill" :data-status="item.status">{{ statusLabel(item.status) }}</span></article>
        </section>

        <section v-else-if="activeTab === 'runs'" class="panel list-panel">
          <header><div><p class="section-kicker">PROCESSING HISTORY</p><h2>持久运行历史</h2></div></header>
          <button v-for="item in query.data.value.runs" :key="item.run_id" class="data-row button-row" @click="item.job_id && navigate(`/processing?job=${encodeURIComponent(item.job_id)}`)"><span><strong>{{ item.pipeline_version }}</strong><small>{{ formatDate(item.created_at) }} · revision {{ item.revision }}</small></span><span><strong>{{ item.current_stage }}</strong><small>{{ Math.round(item.progress * 100) }}%</small></span><span class="status-pill">{{ statusLabel(item.status) }}</span><b>→</b></button>
        </section>
      </template>
    </PageState>

    <div v-if="editing" class="dialog-backdrop" @click.self="editing = null">
      <form class="dialog" @submit.prevent="saveCorrection">
        <p class="section-kicker">CORRECTION OPERATION</p><h2>校正 utterance</h2><p>原始证据不会被覆盖；保存后创建 revision {{ editing.revision + 1 }}。</p>
        <p class="correction-baseline"><strong>模型原始结果 · {{ editing.original_speaker_label ?? '未分配说话人' }} · {{ identityLabel(editing.original_identity) }}</strong>{{ editing.original_text }}</p>
        <label><span>说话人轨道</span><select v-model="draftSpeakerTrackId"><option value="">未分配 / unknown</option><option v-for="track in query.data.value?.speaker_tracks ?? []" :key="track.speaker_track_id" :value="track.speaker_track_id">{{ track.label }}</option></select></label>
        <label><span>本人身份</span><select v-model="draftIdentity"><option value="self">本人</option><option value="not_self">非本人</option><option value="unknown">未知 / 证据不足</option></select></label>
        <label><span>转写文本</span><textarea v-model="draft" rows="5" aria-label="校正文本"></textarea></label>
        <p v-if="saveError" class="form-error">{{ saveError }}</p>
        <div><button type="button" class="quiet-button" @click="editing = null">取消</button><button class="primary-action" :disabled="saving || !draft.trim() || !hasCorrectionChanges()">{{ saving ? '保存中' : '保存新 revision' }}</button></div>
      </form>
    </div>
  </section>
</template>

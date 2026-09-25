<script setup lang="ts">
import VoiceSampleAudition from "../components/VoiceSampleAudition.vue"
import { computed, nextTick, onBeforeUnmount, ref, watchEffect } from 'vue'

import { kindLabel, maturityLabel, matchTierLabel, statusLabel, confirmationLabel, sourceLabel, roleLabel } from '../core/peopleLabels'
import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatDate } from '../core/format'
import { playRange, release } from '../core/media'
import { useQuery } from '../core/query'
import { voiceReviewLane } from '../core/reviews'
import type { PersonDetail, PersonMemory, PersonMemoryKind, SpeakerCluster, VoicePrototypeCandidate } from '../core/types'

const query = useQuery('people', async () => {
  const [clusters, people, sessions, voiceCandidates] = await Promise.all([
    desktopApi.speakerClusters(), desktopApi.people(), desktopApi.sessions(), desktopApi.voicePrototypeCandidates('', 'all'),
  ])
  return { clusters: clusters.items, people: people.items, sessions: sessions.items, voiceCandidates: voiceCandidates.items }
})
const routeParams = new URLSearchParams(window.location.search)
const requestedPersonId = routeParams.get('person') ?? ''
const focusedMemoryId = routeParams.get('memory') ?? ''
const focusedPrototypeId = routeParams.get('prototype') ?? ''
const focusedClusterId = routeParams.get('cluster') ?? ''
const mode = ref<'memory' | 'voice'>(routeParams.get('mode') === 'voice' ? 'voice' : 'memory')
const selectedSession = ref('')
const selectedId = ref('')
const detail = ref<SpeakerCluster | null>(null)
const selectedPersonId = ref('')
const personDetail = ref<PersonDetail | null>(null)
const selectedPerson = ref('')
const newPersonName = ref('')
const mergeSource = ref('')
const splitTracks = ref<string[]>([])
const profileName = ref('')
const profileAliases = ref('')
const profileRelationships = ref('')
const profileNotes = ref('')
const memoryKind = ref<PersonMemoryKind>('stable_fact')
const memorySummary = ref('')
const memoryTopics = ref('')
const memoryEventId = ref('')
const memoryValidUntil = ref('')
const editingMemoryId = ref('')
const editMemorySummary = ref('')
const editMemoryTopics = ref('')
const editMemoryConfidence = ref(1)
const editMemoryValidFrom = ref('')
const editMemoryValidUntil = ref('')
const showMemoryHistory = ref(false)
const busy = ref(false)
const errorMessage = ref('')
const resultMessage = ref('')
const showVoiceReviewHistory = ref(false)
const showVoiceTraining = ref(false)
let focusedDeepLink = false
let focusedCluster = false

watchEffect(() => {
  if (!selectedSession.value && query.data.value?.sessions.length) {
    selectedSession.value = query.data.value.sessions[0].session_id
  }
  const requestedCluster = query.data.value?.clusters.find((cluster) => cluster.cluster_id === focusedClusterId)
  if (!focusedCluster && requestedCluster) {
    focusedCluster = true
    selectedSession.value = requestedCluster.latest_session_id ?? requestedCluster.session_ids[0] ?? selectedSession.value
    void selectCluster(requestedCluster.cluster_id)
  }
  const visible = query.data.value?.clusters.filter(
    (cluster) => !selectedSession.value || cluster.session_ids.includes(selectedSession.value),
  ) ?? []
  if (!visible.some((cluster) => cluster.cluster_id === selectedId.value)) {
    selectedId.value = ''
    detail.value = null
    const nextCluster = visible.find((cluster) => cluster.status === 'active' && !cluster.person_id) ?? visible[0]
    if (nextCluster) void selectCluster(nextCluster.cluster_id)
  }
  if (!selectedPersonId.value && query.data.value?.people.length) {
    const requested = query.data.value.people.find((person) => person.person_id === requestedPersonId)
    void selectPerson(requested?.person_id ?? query.data.value.people[0].person_id)
  }
  const canFocusMemory = focusedMemoryId
    && personDetail.value?.memories.some((memory) => memory.memory_id === focusedMemoryId)
  const canFocusPrototype = focusedPrototypeId
    && query.data.value?.voiceCandidates.some((candidate) => candidate.prototype_id === focusedPrototypeId)
  if (!focusedDeepLink && (canFocusMemory || canFocusPrototype)) {
    focusedDeepLink = true
    const targetId = canFocusMemory
      ? `person-memory-${focusedMemoryId}`
      : `voice-review-${focusedPrototypeId}`
    void nextTick(() => document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
  }
})

const activeClusters = computed(() => query.data.value?.clusters.filter((item) => item.status === 'active') ?? [])
const visibleClusters = computed(() => query.data.value?.clusters.filter(
  (cluster) => !selectedSession.value || cluster.session_ids.includes(selectedSession.value),
) ?? [])
const pendingClusters = computed(() => visibleClusters.value.filter((cluster) => cluster.status === 'active' && !cluster.person_id))
const completedClusters = computed(() => visibleClusters.value.filter((cluster) => cluster.status !== 'active' || !!cluster.person_id))
const mergeChoices = computed(() => activeClusters.value.filter((item) => item.cluster_id !== selectedId.value))
const eventChoices = computed(() => personDetail.value?.interactions.filter((item) => item.event_id) ?? [])
const needsExpiry = computed(() => memoryKind.value === 'short_term_state' || memoryKind.value === 'plan')
const inactiveMemoryCount = computed(() => personDetail.value?.memories.filter((item) => item.status !== 'active').length ?? 0)
const visibleMemories = computed(() => personDetail.value?.memories.filter((item) => showMemoryHistory.value || item.status === 'active') ?? [])
const selectedPersonSummary = computed(() => query.data.value?.people.find((item) => item.person_id === selectedPersonId.value) ?? null)
const unresolvedVoiceCandidates = computed(() => query.data.value?.voiceCandidates.filter((item) => item.review_status === 'pending' || item.review_status === 'uncertain') ?? [])
const pendingVoiceCandidates = computed(() => unresolvedVoiceCandidates.value.filter((item) => voiceReviewLane(item) === 'primary'))
const trainingVoiceCandidates = computed(() => unresolvedVoiceCandidates.value.filter((item) => voiceReviewLane(item) === 'training'))
const excludedVoiceCandidates = computed(() => unresolvedVoiceCandidates.value.filter((item) => voiceReviewLane(item) === null))
const pendingVoiceClusterCount = computed(() => new Set(pendingVoiceCandidates.value.map((item) => item.cluster_id)).size)
const trainingVoiceClusterCount = computed(() => new Set(trainingVoiceCandidates.value.map((item) => item.cluster_id)).size)
const confirmedVoiceCandidates = computed(() => query.data.value?.voiceCandidates.filter((item) => item.review_status === 'confirmed') ?? [])

async function selectCluster(clusterId: string): Promise<void> {
  selectedId.value = clusterId
  splitTracks.value = []
  detail.value = null
  errorMessage.value = ''
  try {
    const selectedDetail = await desktopApi.speakerCluster(clusterId)
    if (selectedId.value === clusterId) detail.value = selectedDetail
  } catch (error) {
    if (selectedId.value === clusterId) errorMessage.value = error instanceof Error ? error.message : String(error)
  }
}

async function selectPerson(personId: string): Promise<void> {
  if (selectedPersonId.value !== personId) showMemoryHistory.value = false
  selectedPersonId.value = personId
  try {
    personDetail.value = await desktopApi.person(personId)
    profileName.value = personDetail.value.display_name
    profileAliases.value = personDetail.value.aliases.join('、')
    profileRelationships.value = personDetail.value.relationship_labels.join('、')
    profileNotes.value = personDetail.value.notes
    memoryEventId.value = eventChoices.value[0]?.event_id ?? ''
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : String(error) }
}

async function act(action: () => Promise<unknown>, message: string): Promise<void> {
  busy.value = true
  errorMessage.value = ''
  resultMessage.value = ''
  try {
    await action()
    if (message) resultMessage.value = message
    await query.refresh(true)
    if (selectedId.value && query.data.value?.clusters.some((cluster) => cluster.cluster_id === selectedId.value)) {
      const clusterId = selectedId.value
      const selectedDetail = await desktopApi.speakerCluster(clusterId).catch(() => null)
      if (selectedId.value === clusterId) detail.value = selectedDetail
    }
    if (selectedPersonId.value) await selectPerson(selectedPersonId.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally { busy.value = false }
}

function analyze(): void {
  if (!selectedSession.value) return
  void act(async () => {
    const result = await desktopApi.analyzeSpeakers(selectedSession.value)
    resultMessage.value = `处理 ${result.embedded_track_count} 条新说话轨：新建 ${result.new_cluster_count} 簇，跨日归并 ${result.matched_track_count} 条；建议 ${result.suggested_cluster_count} 簇，自动识别本人 ${result.auto_identified_self_cluster_count} 簇、已校准人物 ${result.auto_identified_known_cluster_count} 簇。`
  }, '')
}

function refreshMemories(): void {
  if (!selectedPersonId.value) return
  void act(async () => {
    const value = await desktopApi.refreshPersonMemories(selectedPersonId.value)
    resultMessage.value = `人物记忆已重算：新增 ${value.created_count}，修订 ${value.revised_count}，撤回错配 ${value.retracted_count ?? 0}，过期 ${value.expired_count}。`
  }, '')
}

function saveProfile(): void {
  if (!selectedPersonId.value || !profileName.value.trim()) return
  void act(() => desktopApi.updatePersonProfile(selectedPersonId.value, {
    display_name: profileName.value.trim(),
    aliases: splitTags(profileAliases.value),
    relationship_labels: splitTags(profileRelationships.value),
    notes: profileNotes.value,
  }), '人物称呼和关系标签已保存为新版本。')
}

function createMemory(): void {
  if (!selectedPersonId.value || !memorySummary.value.trim() || !memoryEventId.value) return
  if (needsExpiry.value && !memoryValidUntil.value) return
  void act(() => desktopApi.createPersonMemory(selectedPersonId.value, {
    kind: memoryKind.value,
    summary: memorySummary.value.trim(),
    details: { topics: splitTags(memoryTopics.value) },
    confidence: 1,
    valid_from: new Date().toISOString(),
    valid_until: memoryValidUntil.value ? new Date(memoryValidUntil.value).toISOString() : null,
    event_id: memoryEventId.value,
    reminder_event_id: null,
    evidence_utterance_ids: [],
  }), '人物记忆已添加；事件与原话证据保持关联。')
  memorySummary.value = ''
  memoryTopics.value = ''
}

function startEditingMemory(memory: PersonMemory): void {
  editingMemoryId.value = memory.memory_id
  editMemorySummary.value = memory.summary
  editMemoryTopics.value = (memory.details.topics ?? []).join('、')
  editMemoryConfidence.value = memory.confidence
  editMemoryValidFrom.value = localDateTime(memory.valid_from)
  editMemoryValidUntil.value = localDateTime(memory.valid_until)
}

function cancelEditingMemory(): void {
  editingMemoryId.value = ''
}

function saveMemory(memory: PersonMemory): void {
  if (!editMemorySummary.value.trim() || !editMemoryValidFrom.value) return
  const validUntil = editMemoryValidUntil.value ? new Date(editMemoryValidUntil.value).toISOString() : null
  if ((memory.kind === 'short_term_state' || memory.kind === 'plan') && !validUntil) return
  const details = { ...memory.details, topics: splitTags(editMemoryTopics.value) }
  void act(() => desktopApi.revisePersonMemory(memory.memory_id, {
    summary: editMemorySummary.value.trim(), details, confidence: editMemoryConfidence.value,
    valid_from: new Date(editMemoryValidFrom.value).toISOString(), valid_until: validUntil,
    reminder_event_id: memory.reminder_event_id,
  }), '记忆已保存为新版本，旧版本仍可审计。')
  editingMemoryId.value = ''
}

function memoryAction(memory: PersonMemory, action: 'expire' | 'retract' | 'undo'): void {
  if (action === 'expire' && !window.confirm('标记为已过期后，这条内容将不再计入当前有效记忆。继续吗？')) return
  if (action === 'retract' && !window.confirm('撤回表示这条记忆不应继续使用；历史版本和证据仍会保留。继续吗？')) return
  const request = () => action === 'expire' ? desktopApi.expirePersonMemory(memory.memory_id)
    : action === 'retract' ? desktopApi.retractPersonMemory(memory.memory_id)
      : desktopApi.undoPersonMemory(memory.memory_id)
  void act(request, action === 'undo' ? '已撤销最近一次人工修改或状态操作。' : action === 'expire' ? '记忆已标记为过期。' : '记忆已撤回。')
}

function labelExisting(): void {
  if (!detail.value || !selectedPerson.value) return
  void act(() => desktopApi.labelSpeaker(detail.value!.cluster_id, selectedPerson.value), '已确认人物；对应时间线身份、事件和人物记忆已按证据同步。')
}

function createAndLabel(): void {
  if (!detail.value || !newPersonName.value.trim()) return
  void act(() => desktopApi.createAndLabelSpeaker(detail.value!.cluster_id, newPersonName.value.trim()), '已新建人物并关联；候选声纹仍需逐条确认后才会进入稳定库。')
}

function reviewVoice(candidate: VoicePrototypeCandidate, decision: 'confirmed' | 'rejected' | 'uncertain' | 'retracted'): void {
  const message = decision === 'confirmed'
    ? `已确认这条声音属于 ${candidate.person_name}，仅此原型进入稳定声纹库。`
    : decision === 'rejected'
      ? `已记为“不是 ${candidate.person_name}”的负例，不会污染稳定声纹库。`
      : decision === 'retracted'
        ? `已撤回 ${candidate.person_name} 的这条稳定原型，并重新匹配历史声纹。`
        : '已保留待定；不会进入稳定声纹库。'
  void act(() => desktopApi.reviewVoicePrototype(candidate.prototype_id, candidate.person_id, decision), message)
}

function toggleKnownAuto(): void {
  const person = selectedPersonSummary.value
  if (!person || person.kind !== 'known') return
  void act(
    () => desktopApi.updatePersonIdentityPolicy(person.person_id, !person.auto_identity_enabled),
    person.auto_identity_enabled ? '已关闭该人物的自动确认。' : '已开启该人物的严格自动确认。',
  )
}

function merge(): void {
  if (!detail.value || !mergeSource.value) return
  void act(() => desktopApi.mergeSpeakerClusters(detail.value!.cluster_id, [mergeSource.value]), '未知簇已合并，可随时撤销。')
}

function split(): void {
  if (!detail.value || !splitTracks.value.length) return
  void act(() => desktopApi.splitSpeakerCluster(detail.value!.cluster_id, splitTracks.value), '所选说话轨已拆成新的未知簇。')
}

function ignore(): void {
  if (!detail.value || !window.confirm('将此簇标记为电视、广播或环境声？')) return
  void act(() => desktopApi.ignoreSpeakerCluster(detail.value!.cluster_id, 'media_or_environment'), '已忽略；不会进入稳定声纹库。')
}

function undo(): void {
  if (!detail.value) return
  void act(() => desktopApi.undoSpeakerOperation(detail.value!.cluster_id), '已撤销最近一次人工操作；系统生成的时间线身份已同步恢复。')
}

function splitTags(value: string): string[] {
  return [...new Set(value.split(/[，,、]/).map((item) => item.trim()).filter(Boolean))]
}

function evidenceLabel(memory: PersonMemory, evidence: PersonMemory['evidence'][number]): string {
  const parts = memory.evidence.filter((item) => item.utterance_id === evidence.utterance_id)
  if (parts.length <= 1) return `原话：${evidence.text}`
  const index = parts.findIndex((item) => item.evidence_span_id === evidence.evidence_span_id)
  return `原话片段 ${index + 1}/${parts.length}：${evidence.text}`
}

function sessionLabel(sessionId: string): string {
  const session = query.data.value?.sessions.find((item) => item.session_id === sessionId)
  return session ? `${formatDate(session.captured_start)} · ${sessionId.slice(0, 8)}` : sessionId.slice(0, 8)
}

function localDateTime(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 16)
}

onBeforeUnmount(release)
</script>

<template>
  <section class="page people-page">
    <header class="page-header">
      <div><p class="overline">PEOPLE · VOICE · MEMORY</p><h1>人物与<em>记忆</em></h1></div>
      <span class="header-note">事实、短期状态和模型观察分开保存；每条记忆都能回到事件和原话</span>
    </header>

    <nav class="people-mode-tabs">
      <button :class="{ active: mode === 'memory' }" @click="mode = 'memory'">跨天人物记忆</button>
      <button :class="{ active: mode === 'voice' }" @click="mode = 'voice'">未知声纹聚类</button>
    </nav>

    <template v-if="mode === 'voice'">
      <section class="panel speaker-runner">
        <div><p class="section-kicker">LAYERED SPEAKER IDENTITY</p><h2>定位并分析一段录音</h2><p>证据不足、自动匹配、建议核对、未匹配已知人物分档处理。只有已校准且主动开启的人物才能自动确认。</p></div>
        <label><span>录音会话</span><select v-model="selectedSession"><option value="" disabled>选择会话</option><option v-for="session in query.data.value?.sessions" :key="session.session_id" :value="session.session_id">{{ formatDate(session.captured_start) }} · {{ session.session_id.slice(0, 8) }}</option></select></label>
        <button class="primary-action" :disabled="!selectedSession || busy" @click="analyze">{{ busy ? '本机分析中…' : '聚类并重新匹配身份' }}</button>
      </section>
      <section class="panel voice-review-queue">
        <header><div><p class="section-kicker">HUMAN-GATED LEARNING</p><h2>高质量声纹审核</h2><p>主审核按声音聚类计数；这里保留逐个原型的试听和确认能力。</p></div><div><span class="status-pill">{{ pendingVoiceClusterCount }} 组待核对 · {{ pendingVoiceCandidates.length }} 个样本</span><button v-if="trainingVoiceClusterCount" class="text-button" @click="showVoiceTraining = !showVoiceTraining">{{ showVoiceTraining ? '隐藏可选训练' : `可选训练 ${trainingVoiceClusterCount} 组` }}</button><button class="text-button" @click="showVoiceReviewHistory = !showVoiceReviewHistory">{{ showVoiceReviewHistory ? '隐藏已确认' : `查看已确认 ${confirmedVoiceCandidates.length}` }}</button></div></header>
        <div v-if="unresolvedVoiceCandidates.length" class="review-count-ledger" aria-label="声纹候选分流口径">
          <strong>声纹候选 {{ unresolvedVoiceCandidates.length }} 个</strong>
          <span>主审核 {{ pendingVoiceCandidates.length }} 个样本 → {{ pendingVoiceClusterCount }} 组</span>
          <span>可选训练 {{ trainingVoiceCandidates.length }} 个样本 → {{ trainingVoiceClusterCount }} 组</span>
          <span>暂不进入审核 {{ excludedVoiceCandidates.length }} 个</span>
        </div>
        <div class="voice-review-list">
          <article v-for="candidate in pendingVoiceCandidates" :id="`voice-review-${candidate.prototype_id}`" :key="candidate.prototype_id" :class="['voice-review-card', candidate.prototype_id === focusedPrototypeId ? 'focused-review' : '']">
            <div><strong>{{ candidate.person_name }}</strong><small>{{ matchTierLabel(candidate.decision_tier) }} · 音频质量 {{ Math.round(candidate.quality_score * 100) }}%<template v-if="candidate.best_score !== null"> · 最相似 {{ Math.round(candidate.best_score * 100) }}%</template></small><small>录音 {{ sessionLabel(candidate.session_id) }}</small></div>
            <VoiceSampleAudition :candidate="candidate" v-slot="{ complete }">
            <footer><button class="quiet-button" :disabled="busy" @click="reviewVoice(candidate, 'uncertain')">待定</button><button class="text-danger" :disabled="busy || !complete" @click="reviewVoice(candidate, 'rejected')">不是此人</button><button class="primary-action" :disabled="busy || !complete" @click="reviewVoice(candidate, 'confirmed')">确认是此人</button></footer>
            </VoiceSampleAudition>
          </article>
          <article v-for="candidate in showVoiceTraining ? trainingVoiceCandidates : []" :id="`voice-review-${candidate.prototype_id}`" :key="`training-${candidate.prototype_id}`" :class="['voice-review-card', candidate.prototype_id === focusedPrototypeId ? 'focused-review' : '']">
            <div><strong>{{ candidate.person_name }} · 可选训练</strong><small>{{ matchTierLabel(candidate.decision_tier) }} · 音频质量 {{ Math.round(candidate.quality_score * 100) }}%<template v-if="candidate.best_score !== null"> · 最相似 {{ Math.round(candidate.best_score * 100) }}%</template></small><small>录音 {{ sessionLabel(candidate.session_id) }}</small></div>
            <VoiceSampleAudition :candidate="candidate" v-slot="{ complete }">
            <footer><button class="text-danger" :disabled="busy || !complete" @click="reviewVoice(candidate, 'rejected')">不是此人</button><button class="primary-action" :disabled="busy || !complete" @click="reviewVoice(candidate, 'confirmed')">确认是此人</button></footer>
            </VoiceSampleAudition>
          </article>
          <article v-for="candidate in showVoiceReviewHistory ? confirmedVoiceCandidates : []" :key="`confirmed-${candidate.prototype_id}`" class="voice-review-card confirmed">
            <div><strong>{{ candidate.person_name }}</strong><small>已人工确认 · 音频质量 {{ Math.round(candidate.quality_score * 100) }}%</small><small>录音 {{ sessionLabel(candidate.session_id) }}</small></div>
            <VoiceSampleAudition :candidate="candidate">
            <footer><button class="text-danger" :disabled="busy" @click="reviewVoice(candidate, 'retracted')">撤回此原型</button></footer>
            </VoiceSampleAudition>
          </article>
          <p v-if="!pendingVoiceCandidates.length && !showVoiceReviewHistory" class="empty-inline">目前没有满足质量门槛的待审核原型。</p>
        </div>
      </section>
    </template>

    <p v-if="errorMessage" class="form-error speaker-message">{{ errorMessage }}</p>
    <p v-if="resultMessage" class="generation-message">{{ resultMessage }}</p>

    <PageState v-if="mode === 'memory'" :loading="query.loading.value" :error="query.error.value" :empty="!query.data.value?.people.length" empty-text="还没有已确认人物。请先在“未知声纹聚类”中确认人物身份。" @retry="query.refresh(true)">
      <div class="people-layout memory-layout">
        <aside class="panel cluster-list person-list">
          <button v-for="person in query.data.value?.people" :key="person.person_id" :class="{ active: selectedPersonId === person.person_id }" @click="selectPerson(person.person_id)">
            <span class="speaker-mark">{{ person.display_name.slice(0, 1) }}</span>
            <span><strong>{{ person.display_name }}</strong><small>{{ person.interaction_count }} 次跨天互动 · {{ person.memory_count }} 条有效记忆</small><small v-if="person.kind === 'self'">{{ person.enrollment_reference_count }} 条注册声纹 · {{ person.auto_identity_enabled ? '自动识别已开启' : '自动识别未就绪' }}</small><small v-else>声纹{{ maturityLabel(person.voice_maturity_status) }} · {{ person.prototype_count }} 正例 / {{ person.rejected_prototype_count }} 负例 · {{ person.pending_voice_review_count }} 组待核对<template v-if="person.training_voice_review_count"> · {{ person.training_voice_review_count }} 组可选训练</template></small><small v-if="person.last_interaction_at">最近 {{ formatDate(person.last_interaction_at) }}</small></span>
            <i>{{ person.kind }}</i>
          </button>
        </aside>

        <section v-if="personDetail" class="person-memory-workspace">
          <article class="panel person-memory-hero">
            <header><div><p class="section-kicker">PERSON {{ personDetail.person_id.slice(0, 8) }}</p><h2>{{ personDetail.display_name }}</h2><p>{{ personDetail.aliases.join(' · ') || '暂无别名' }} · {{ personDetail.relationship_labels.join(' · ') || '关系待补充' }}</p></div><button class="quiet-button" :disabled="busy" @click="refreshMemories">从事件层重算</button></header>
            <div class="cluster-metrics"><span><small>首次出现</small><strong>{{ personDetail.first_seen_at ? formatDate(personDetail.first_seen_at) : '—' }}</strong></span><span><small>最近互动</small><strong>{{ personDetail.last_seen_at ? formatDate(personDetail.last_seen_at) : '—' }}</strong></span><span><small>互动</small><strong>{{ personDetail.interaction_count }}</strong></span><span><small>有效记忆</small><strong>{{ personDetail.memory_count }}</strong></span></div>
            <div class="topic-strip"><span v-for="topic in personDetail.topics" :key="topic.label">{{ topic.label }} · {{ topic.count }}</span><small v-if="!personDetail.topics.length">还没有共同话题</small></div>
          </article>

          <article v-if="selectedPersonSummary?.kind === 'known'" class="panel voice-policy-card">
            <header><div><p class="section-kicker">VOICE IDENTITY POLICY</p><h3>声纹{{ maturityLabel(selectedPersonSummary.voice_maturity_status) }}</h3><p>正例 {{ selectedPersonSummary.voice_calibration.positive_count ?? 0 }} 条 / {{ selectedPersonSummary.voice_calibration.positive_session_count ?? 0 }} 段录音；困难负例 {{ selectedPersonSummary.voice_calibration.negative_count ?? 0 }} 条。</p></div><button class="quiet-button" :disabled="busy || selectedPersonSummary.voice_maturity_status !== 'calibrated'" @click="toggleKnownAuto">{{ selectedPersonSummary.auto_identity_enabled ? '关闭自动确认' : '开启严格自动确认' }}</button></header>
            <p v-if="selectedPersonSummary.voice_maturity_status !== 'calibrated'">校准门槛：至少 3 条人工确认正例、跨 2 段录音、2 条人工拒绝负例；跨录音召回不低于 80%，负例误接收为 0。</p><p v-else>已通过日常聊天样本校准。低于建议阈值仍保持“其他人”，只有同时超过 {{ Math.round((selectedPersonSummary.auto_accept_threshold ?? 0) * 100) }}% 且与第二候选拉开差距才会自动确认。</p>
          </article>

          <div class="person-memory-grid">
            <article class="panel profile-editor"><h3>基本身份</h3><label><span>显示名称</span><input v-model="profileName" /></label><label><span>别名和称呼</span><input v-model="profileAliases" placeholder="老张、张同学" /></label><label><span>关系标签</span><input v-model="profileRelationships" placeholder="项目成员、同学" /></label><label><span>备注</span><textarea v-model="profileNotes" rows="3" /></label><button class="primary-action" :disabled="busy || !profileName.trim()" @click="saveProfile">保存人物资料</button></article>

            <article class="panel profile-editor"><h3>添加有证据的记忆</h3><label><span>记忆类型</span><select v-model="memoryKind"><option value="stable_fact">稳定事实</option><option value="preference">偏好</option><option value="short_term_state">短期状态</option><option value="plan">计划</option><option value="commitment">承诺</option><option value="model_observation">模型观察</option></select></label><label><span>内容</span><input v-model="memorySummary" placeholder="例如：是合同项目成员" /></label><label><span>证据事件</span><select v-model="memoryEventId"><option value="">选择一条互动事件</option><option v-for="event in eventChoices" :key="event.event_id!" :value="event.event_id!">{{ event.title }}</option></select></label><label><span>话题</span><input v-model="memoryTopics" placeholder="合同、项目进度" /></label><label v-if="needsExpiry"><span>有效至</span><input v-model="memoryValidUntil" type="datetime-local" /></label><button class="primary-action" :disabled="busy || !memorySummary.trim() || !memoryEventId || (needsExpiry && !memoryValidUntil)" @click="createMemory">添加记忆</button></article>
          </div>

          <article class="panel memory-section">
            <header>
              <div><p class="section-kicker">MEMORY LEDGER</p><h3>事实、状态、计划与观察</h3></div>
              <div class="memory-section-tools">
                <small>这里只保存跨天仍值得使用的人物信息；“待核对”和“模型推断”不会冒充已确认事实。</small>
                <button v-if="inactiveMemoryCount" class="quiet-button" @click="showMemoryHistory = !showMemoryHistory">{{ showMemoryHistory ? '隐藏历史' : `查看历史（${inactiveMemoryCount}）` }}</button>
              </div>
            </header>
            <div class="memory-card-list">
              <section v-for="memory in visibleMemories" :id="`person-memory-${memory.memory_id}`" :key="`${memory.memory_id}-${memory.revision}`" :class="['memory-card', memory.status, memory.kind === 'model_observation' ? 'observation' : '', memory.memory_id === focusedMemoryId ? 'focused-review' : '']">
                <header>
                  <span>{{ kindLabel(memory.kind) }}</span>
                  <i>{{ confirmationLabel(memory) }} · {{ statusLabel(memory) }} · r{{ memory.revision }}</i>
                </header>
                <template v-if="editingMemoryId !== memory.memory_id">
                  <h4>{{ memory.summary }}</h4>
                  <dl class="memory-metadata">
                    <div><dt>来源</dt><dd>{{ sourceLabel(memory) }}</dd></div>
                    <div><dt>人物角色</dt><dd>{{ roleLabel(memory) }}</dd></div>
                    <div><dt>可信度</dt><dd>{{ Math.round(memory.confidence * 100) }}%</dd></div>
                    <div><dt>有效期</dt><dd>{{ formatDate(memory.valid_from) }} → {{ memory.valid_until ? formatDate(memory.valid_until) : '持续有效，等待新证据修订' }}</dd></div>
                  </dl>
                  <div v-if="memory.details.topics?.length" class="memory-topics"><span v-for="topic in memory.details.topics" :key="topic">{{ topic }}</span></div>
                </template>
                <div v-else class="memory-card-editor">
                  <label><span>内容</span><textarea v-model="editMemorySummary" rows="3" /></label>
                  <label><span>话题</span><input v-model="editMemoryTopics" placeholder="用逗号分隔" /></label>
                  <div class="memory-editor-row">
                    <label><span>可信度</span><input v-model.number="editMemoryConfidence" type="number" min="0" max="1" step="0.05" /></label>
                    <label><span>有效起始</span><input v-model="editMemoryValidFrom" type="datetime-local" /></label>
                    <label><span>有效至</span><input v-model="editMemoryValidUntil" type="datetime-local" /></label>
                  </div>
                  <p v-if="memory.confirmation_status !== 'confirmed'">保存人工核对结果后，此版本会标为“已确认”。</p>
                  <div class="memory-editor-actions"><button class="primary-action" :disabled="busy || !editMemorySummary.trim() || !editMemoryValidFrom || ((memory.kind === 'short_term_state' || memory.kind === 'plan') && !editMemoryValidUntil)" @click="saveMemory(memory)">保存新版本</button><button class="quiet-button" :disabled="busy" @click="cancelEditingMemory">取消</button></div>
                </div>
                <div v-if="memory.reminder" class="memory-reminder">提醒 · {{ memory.reminder.title }} · {{ memory.reminder.status }}</div>
                <div class="memory-evidence">
                  <template v-for="(evidence, evidenceIndex) in memory.evidence.filter((item) => item.utterance_id)" :key="evidence.evidence_span_id ?? `${evidence.utterance_id}-${evidenceIndex}`">
                    <button v-if="evidence.media_id && evidence.start_ms !== null && evidence.end_ms !== null" class="quiet-button" @click="playRange(evidence.media_id, evidence.start_ms, evidence.end_ms)">▶ {{ evidenceLabel(memory, evidence) }}</button>
                    <button v-else class="quiet-button" disabled title="文字证据已经保留，但尚未找到对应音频区间">音频待映射 · {{ evidenceLabel(memory, evidence) }}</button>
                  </template>
                  <span v-if="memory.event_id">证据事件 {{ memory.event_id.slice(0, 8) }}</span>
                  <span v-if="!memory.evidence.some((item) => item.utterance_id)">仅有关联事件，暂无原话证据</span>
                </div>
                <footer v-if="editingMemoryId !== memory.memory_id">
                  <button class="quiet-button" :disabled="busy || !memory.available_actions.can_revise" :title="memory.available_actions.can_revise ? '' : '仅当前有效的记忆可以修改'" @click="startEditingMemory(memory)">{{ memory.confirmation_status === 'confirmed' ? '修改' : '核对并修改' }}</button>
                  <button class="quiet-button" :disabled="busy || !memory.available_actions.can_expire" title="表示内容曾经有效，但现在已不再有效" @click="memoryAction(memory, 'expire')">标记已过期</button>
                  <button class="text-danger" :disabled="busy || !memory.available_actions.can_retract" title="表示这条记忆不应继续使用" @click="memoryAction(memory, 'retract')">撤回</button>
                  <button class="quiet-button" :disabled="busy || !memory.available_actions.can_undo" :title="memory.available_actions.can_undo ? '撤销最近一次人工修改或状态操作' : '没有可撤销的人工操作'" @click="memoryAction(memory, 'undo')">撤销最近操作</button>
                </footer>
              </section>
              <p v-if="!visibleMemories.length" class="empty-inline">当前没有有效记忆；相关事件仍保留在下方“跨天互动”中。你也可以添加一条带证据的记忆。</p>
            </div>
          </article>

          <div class="person-memory-grid bottom-grid">
            <article class="panel memory-section"><header><div><p class="section-kicker">OPEN COMMITMENTS</p><h3>未完成承诺</h3></div></header><div class="compact-memory-list"><p v-for="memory in personDetail.commitments" :key="memory.memory_id"><strong>{{ memory.summary }}</strong><small>{{ memory.details.commitment_direction || '方向待确认' }}<template v-if="memory.reminder"> · 已关联提醒 {{ formatDate(memory.reminder.scheduled_at) }}</template></small></p><span v-if="!personDetail.commitments.length">没有有效的未完成承诺</span></div></article>
            <article class="panel memory-section"><header><div><p class="section-kicker">INTERACTION TIMELINE</p><h3>跨天互动</h3></div></header><div class="interaction-list"><p v-for="interaction in personDetail.interactions" :key="`${interaction.interaction_type}-${interaction.event_id ?? interaction.session_id}`"><time>{{ formatDate(interaction.occurred_at) }}</time><strong>{{ interaction.title }}</strong><small>{{ interaction.interaction_type === 'event' ? interaction.event_kind : `${interaction.utterance_count ?? 0} 条原话` }} · 会话 {{ interaction.session_id.slice(0, 8) }}</small></p></div></article>
          </div>
        </section>
      </div>
    </PageState>

    <PageState v-else :loading="query.loading.value" :error="query.error.value" :empty="!visibleClusters.length" empty-text="所选录音还没有说话人簇。点击“聚类并重新匹配身份”开始本机分析。" @retry="query.refresh(true)">
      <div class="people-layout">
        <aside class="panel cluster-list">
          <div class="cluster-list-section">
            <h3>需要处理 <span>{{ pendingClusters.length }}</span></h3>
            <p v-if="!pendingClusters.length" class="cluster-list-empty">当前录音没有待处理的声纹簇。</p>
            <button v-for="cluster in pendingClusters" :key="cluster.cluster_id" :class="{ active: selectedId === cluster.cluster_id }" @click="selectCluster(cluster.cluster_id)"><span class="speaker-mark">{{ (cluster.person_name ?? cluster.suggested_person_name)?.slice(0, 1) ?? '?' }}</span><span><strong>{{ cluster.person_name ?? cluster.display_label }}</strong><small>共 {{ cluster.session_count }} 段录音 / {{ cluster.track_count }} 条说话轨</small><small v-if="cluster.person_name">已关联 {{ cluster.person_name }}</small><small v-else-if="cluster.status === 'ignored'">已忽略 · 可撤销</small><small v-else-if="cluster.status !== 'active'">已归档</small><small v-else-if="cluster.suggested_person_id">建议核对 {{ cluster.suggested_person_name ?? '候选人物' }}</small><small v-else>等待确认或排除</small></span></button>
          </div>
          <div v-if="completedClusters.length" class="cluster-list-section completed-clusters">
            <h3>已处理与已排除 <span>{{ completedClusters.length }}</span></h3>
            <button v-for="cluster in completedClusters" :key="cluster.cluster_id" :class="{ active: selectedId === cluster.cluster_id }" @click="selectCluster(cluster.cluster_id)"><span class="speaker-mark">{{ (cluster.person_name ?? cluster.suggested_person_name)?.slice(0, 1) ?? '?' }}</span><span><strong>{{ cluster.person_name ?? cluster.display_label }}</strong><small>共 {{ cluster.session_count }} 段录音 / {{ cluster.track_count }} 条说话轨</small><small v-if="cluster.person_name">已关联 {{ cluster.person_name }}</small><small v-else-if="cluster.status === 'ignored'">已忽略 · 可撤销</small><small v-else-if="cluster.status !== 'active'">已归档</small><small v-else-if="cluster.suggested_person_id">建议核对 {{ cluster.suggested_person_name ?? '候选人物' }}</small><small v-else>等待确认或排除</small></span></button>
          </div>
        </aside>
        <section v-if="detail" class="cluster-workspace">
          <article class="panel cluster-hero"><header><div><p class="section-kicker">CLUSTER {{ detail.cluster_id.slice(0, 8) }}</p><h2>{{ detail.person_name ?? detail.display_label }}</h2></div><span class="status-pill">{{ detail.link_source === 'automatic' ? '本人声纹自动识别' : detail.person_name ? '人工已确认' : detail.suggested_person_id ? '可能匹配' : '暂无法判断' }}</span></header><div class="cluster-metrics"><span><small>跨录音</small><strong>{{ detail.session_count }}</strong></span><span><small>说话轨</small><strong>{{ detail.track_count }}</strong></span><span><small>候选原型</small><strong>{{ detail.prototypes?.filter((item) => item.status === 'candidate').length ?? 0 }}</strong></span><span><small>版本</small><strong>r{{ detail.revision }}</strong></span></div><div class="topic-strip"><span v-for="sessionId in detail.session_ids" :key="sessionId">来源 · {{ sessionLabel(sessionId) }}</span></div><div class="representative-clips"><small>代表片段</small><button v-for="clip in detail.prototypes?.flatMap((item) => item.representative_clips).slice(0, 5)" :key="`${clip.media_id}-${clip.start_ms}`" class="quiet-button" @click="playRange(clip.media_id, clip.start_ms, clip.end_ms)">▶ {{ Math.round((clip.end_ms - clip.start_ms) / 1000) }} 秒</button><span v-if="!detail.prototypes?.length">暂无可播放片段</span></div></article>
          <div v-if="detail.status === 'ignored'" class="state-panel">
            此簇已忽略，需先恢复才能确认身份或修改聚类。
            <button class="primary-action" :disabled="busy" @click="undo">{{ busy ? '恢复中…' : '撤销最近操作并恢复' }}</button>
          </div>
          <p v-else-if="detail.status !== 'active'" class="state-panel">此簇已归档，请选择活跃簇继续操作。</p>
          <div v-else class="cluster-actions-grid">
            <article class="panel identity-action"><h3>确认人物身份</h3><p>这里确认的是人物关联和时间线身份，不会批量注册簇内声纹。稳定声纹必须在上方逐条试听确认。</p><label><span>已有联系人</span><select v-model="selectedPerson"><option value="">选择人物</option><option v-for="person in query.data.value?.people" :key="person.person_id" :value="person.person_id">{{ person.display_name }} · {{ person.kind === 'self' ? `${person.enrollment_reference_count} 注册声纹` : `${person.prototype_count} 条已确认原型` }}</option></select></label><button class="primary-action" :disabled="detail.status !== 'active' || !selectedPerson || busy" @click="labelExisting">关联已有</button><label><span>或新建人物</span><input v-model="newPersonName" placeholder="姓名或称呼" /></label><button class="quiet-button" :disabled="detail.status !== 'active' || !newPersonName.trim() || busy" @click="createAndLabel">新建并关联</button></article>
            <article class="panel identity-action"><h3>纠正聚类</h3><p>合并和拆分只移动匿名说话轨，原始识别产物保持不变。</p><label><span>合并另一个未知簇</span><select v-model="mergeSource"><option value="">选择来源簇</option><option v-for="cluster in mergeChoices" :key="cluster.cluster_id" :value="cluster.cluster_id">{{ cluster.person_name ?? cluster.display_label }}</option></select></label><button class="quiet-button" :disabled="detail.status !== 'active' || !mergeSource || busy" @click="merge">合并到当前簇</button><fieldset><legend>拆出说话轨</legend><label v-for="member in detail.members" :key="member.speaker_track_id" class="track-check"><input v-model="splitTracks" type="checkbox" :value="member.speaker_track_id" />{{ member.label }} · {{ member.session_id.slice(0, 8) }}</label></fieldset><button class="quiet-button" :disabled="detail.status !== 'active' || !splitTracks.length || splitTracks.length === detail.members?.length || busy" @click="split">拆成新簇</button></article>
            <article class="panel identity-action safety-action"><h3>保留未知或排除</h3><p>没有足够证据时无需操作，系统会继续保留匿名簇。电视、广播和环境声可以明确排除。</p><button class="quiet-button" disabled>暂无法判断（保持未知）</button><button class="quiet-button" :disabled="detail.status !== 'active' || busy" @click="ignore">忽略媒体 / 环境声</button><button class="text-danger" :disabled="busy" @click="undo">撤销最近操作</button></article>
          </div>
        </section>
      </div>
    </PageState>
  </section>
</template>

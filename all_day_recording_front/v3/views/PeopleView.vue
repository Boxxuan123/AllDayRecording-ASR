<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watchEffect } from 'vue'

import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatDate } from '../core/format'
import { playRange, release } from '../core/media'
import { useQuery } from '../core/query'
import type { PersonDetail, PersonMemory, PersonMemoryKind, SpeakerCluster } from '../core/types'

const query = useQuery('people', async () => {
  const [clusters, people, sessions] = await Promise.all([
    desktopApi.speakerClusters(), desktopApi.people(), desktopApi.sessions(),
  ])
  return { clusters: clusters.items, people: people.items, sessions: sessions.items }
})
const mode = ref<'memory' | 'voice'>('memory')
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
const busy = ref(false)
const errorMessage = ref('')
const resultMessage = ref('')

watchEffect(() => {
  if (!selectedSession.value && query.data.value?.sessions.length) {
    selectedSession.value = query.data.value.sessions[0].session_id
  }
  if (!selectedId.value && query.data.value?.clusters.length) {
    void selectCluster(query.data.value.clusters[0].cluster_id)
  }
  if (!selectedPersonId.value && query.data.value?.people.length) {
    void selectPerson(query.data.value.people[0].person_id)
  }
})

const activeClusters = computed(() => query.data.value?.clusters.filter((item) => item.status === 'active') ?? [])
const mergeChoices = computed(() => activeClusters.value.filter((item) => item.cluster_id !== selectedId.value))
const eventChoices = computed(() => personDetail.value?.interactions.filter((item) => item.event_id) ?? [])
const needsExpiry = computed(() => memoryKind.value === 'short_term_state' || memoryKind.value === 'plan')

async function selectCluster(clusterId: string): Promise<void> {
  selectedId.value = clusterId
  splitTracks.value = []
  try { detail.value = await desktopApi.speakerCluster(clusterId) }
  catch (error) { errorMessage.value = error instanceof Error ? error.message : String(error) }
}

async function selectPerson(personId: string): Promise<void> {
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
    resultMessage.value = message
    await query.refresh(true)
    if (selectedId.value) detail.value = await desktopApi.speakerCluster(selectedId.value).catch(() => null)
    if (selectedPersonId.value) await selectPerson(selectedPersonId.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : String(error)
  } finally { busy.value = false }
}

function analyze(): void {
  if (!selectedSession.value) return
  void act(async () => {
    const result = await desktopApi.analyzeSpeakers(selectedSession.value)
    resultMessage.value = `处理 ${result.embedded_track_count} 条说话轨：新建 ${result.new_cluster_count} 簇，跨日归并 ${result.matched_track_count} 条。`
  }, '')
}

function refreshMemories(): void {
  if (!selectedPersonId.value) return
  void act(async () => {
    const value = await desktopApi.refreshPersonMemories(selectedPersonId.value)
    resultMessage.value = `人物记忆已重算：新增 ${value.created_count}，修订 ${value.revised_count}，过期 ${value.expired_count}。`
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

function reviseMemory(memory: PersonMemory): void {
  const summary = window.prompt('修改记忆内容', memory.summary)
  if (!summary?.trim()) return
  void act(() => desktopApi.revisePersonMemory(memory.memory_id, {
    summary: summary.trim(), details: memory.details, confidence: memory.confidence,
    valid_from: memory.valid_from, valid_until: memory.valid_until,
    reminder_event_id: memory.reminder_event_id,
  }), '记忆已保存为新版本，旧版本仍可审计。')
}

function memoryAction(memory: PersonMemory, action: 'expire' | 'retract' | 'undo'): void {
  const request = action === 'expire' ? desktopApi.expirePersonMemory(memory.memory_id)
    : action === 'retract' ? desktopApi.retractPersonMemory(memory.memory_id)
      : desktopApi.undoPersonMemory(memory.memory_id)
  void act(() => request, action === 'undo' ? '已撤销最近一次记忆操作。' : action === 'expire' ? '记忆已过期。' : '记忆已撤回。')
}

function labelExisting(): void {
  if (!detail.value || !selectedPerson.value) return
  void act(() => desktopApi.labelSpeaker(detail.value!.cluster_id, selectedPerson.value), '已确认人物；相关事件和人物记忆会按证据迁移。')
}

function createAndLabel(): void {
  if (!detail.value || !newPersonName.value.trim()) return
  void act(() => desktopApi.createAndLabelSpeaker(detail.value!.cluster_id, newPersonName.value.trim()), '已新建人物并确认；候选原型已进入稳定声纹库。')
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
  void act(() => desktopApi.undoSpeakerOperation(detail.value!.cluster_id), '已撤销最近一次人工操作。')
}

function splitTags(value: string): string[] {
  return [...new Set(value.split(/[，,、]/).map((item) => item.trim()).filter(Boolean))]
}

function kindLabel(kind: PersonMemoryKind): string {
  return { stable_fact: '稳定事实', preference: '偏好', short_term_state: '短期状态', plan: '计划', commitment: '承诺', model_observation: '模型观察' }[kind]
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
        <div><p class="section-kicker">LOCAL CAM++</p><h2>分析一段录音</h2><p>音频仅在本机读取。重复人物会跨录音聚合，人物相似只显示为“可能”。</p></div>
        <label><span>录音会话</span><select v-model="selectedSession"><option value="" disabled>选择会话</option><option v-for="session in query.data.value?.sessions" :key="session.session_id" :value="session.session_id">{{ formatDate(session.captured_start) }} · {{ session.session_id.slice(0, 8) }}</option></select></label>
        <button class="primary-action" :disabled="!selectedSession || busy" @click="analyze">{{ busy ? '本机分析中…' : '开始聚类' }}</button>
      </section>
    </template>

    <p v-if="errorMessage" class="form-error speaker-message">{{ errorMessage }}</p>
    <p v-if="resultMessage" class="generation-message">{{ resultMessage }}</p>

    <PageState v-if="mode === 'memory'" :loading="query.loading.value" :error="query.error.value" :empty="!query.data.value?.people.length" empty-text="还没有已确认人物。请先在“未知声纹聚类”中确认人物身份。" @retry="query.refresh(true)">
      <div class="people-layout memory-layout">
        <aside class="panel cluster-list person-list">
          <button v-for="person in query.data.value?.people" :key="person.person_id" :class="{ active: selectedPersonId === person.person_id }" @click="selectPerson(person.person_id)">
            <span class="speaker-mark">{{ person.display_name.slice(0, 1) }}</span>
            <span><strong>{{ person.display_name }}</strong><small>{{ person.interaction_count }} 次跨天互动 · {{ person.memory_count }} 条有效记忆</small><small v-if="person.last_interaction_at">最近 {{ formatDate(person.last_interaction_at) }}</small></span>
            <i>{{ person.kind }}</i>
          </button>
        </aside>

        <section v-if="personDetail" class="person-memory-workspace">
          <article class="panel person-memory-hero">
            <header><div><p class="section-kicker">PERSON {{ personDetail.person_id.slice(0, 8) }}</p><h2>{{ personDetail.display_name }}</h2><p>{{ personDetail.aliases.join(' · ') || '暂无别名' }} · {{ personDetail.relationship_labels.join(' · ') || '关系待补充' }}</p></div><button class="quiet-button" :disabled="busy" @click="refreshMemories">从事件层重算</button></header>
            <div class="cluster-metrics"><span><small>首次出现</small><strong>{{ personDetail.first_seen_at ? formatDate(personDetail.first_seen_at) : '—' }}</strong></span><span><small>最近互动</small><strong>{{ personDetail.last_seen_at ? formatDate(personDetail.last_seen_at) : '—' }}</strong></span><span><small>互动</small><strong>{{ personDetail.interaction_count }}</strong></span><span><small>有效记忆</small><strong>{{ personDetail.memory_count }}</strong></span></div>
            <div class="topic-strip"><span v-for="topic in personDetail.topics" :key="topic.label">{{ topic.label }} · {{ topic.count }}</span><small v-if="!personDetail.topics.length">还没有共同话题</small></div>
          </article>

          <div class="person-memory-grid">
            <article class="panel profile-editor"><h3>基本身份</h3><label><span>显示名称</span><input v-model="profileName" /></label><label><span>别名和称呼</span><input v-model="profileAliases" placeholder="老张、张同学" /></label><label><span>关系标签</span><input v-model="profileRelationships" placeholder="项目成员、同学" /></label><label><span>备注</span><textarea v-model="profileNotes" rows="3" /></label><button class="primary-action" :disabled="busy || !profileName.trim()" @click="saveProfile">保存人物资料</button></article>

            <article class="panel profile-editor"><h3>添加有证据的记忆</h3><label><span>记忆类型</span><select v-model="memoryKind"><option value="stable_fact">稳定事实</option><option value="preference">偏好</option><option value="short_term_state">短期状态</option><option value="plan">计划</option><option value="commitment">承诺</option><option value="model_observation">模型观察</option></select></label><label><span>内容</span><input v-model="memorySummary" placeholder="例如：是合同项目成员" /></label><label><span>证据事件</span><select v-model="memoryEventId"><option value="">选择一条互动事件</option><option v-for="event in eventChoices" :key="event.event_id!" :value="event.event_id!">{{ event.title }}</option></select></label><label><span>话题</span><input v-model="memoryTopics" placeholder="合同、项目进度" /></label><label v-if="needsExpiry"><span>有效至</span><input v-model="memoryValidUntil" type="datetime-local" /></label><button class="primary-action" :disabled="busy || !memorySummary.trim() || !memoryEventId || (needsExpiry && !memoryValidUntil)" @click="createMemory">添加记忆</button></article>
          </div>

          <article class="panel memory-section"><header><div><p class="section-kicker">MEMORY LEDGER</p><h3>事实、状态、计划与观察</h3></div><small>模型观察不会显示为已确认事实</small></header><div class="memory-card-list"><section v-for="memory in personDetail.memories" :key="`${memory.memory_id}-${memory.revision}`" :class="['memory-card', memory.status, memory.kind === 'model_observation' ? 'observation' : '']"><header><span>{{ kindLabel(memory.kind) }}</span><i>{{ memory.confirmation_status === 'inferred' ? '推断' : memory.status }} · r{{ memory.revision }}</i></header><h4>{{ memory.summary }}</h4><p>有效：{{ formatDate(memory.valid_from) }} → {{ memory.valid_until ? formatDate(memory.valid_until) : '未预设永久，待后续证据修订' }}</p><div v-if="memory.reminder" class="memory-reminder">提醒 · {{ memory.reminder.title }} · {{ memory.reminder.status }}</div><div class="memory-evidence"><button v-for="evidence in memory.evidence.filter((item) => item.media_id)" :key="evidence.utterance_id!" class="quiet-button" @click="playRange(evidence.media_id!, evidence.start_ms!, evidence.end_ms!)">▶ 原话：{{ evidence.text }}</button><span v-if="memory.event_id">事件 {{ memory.event_id.slice(0, 8) }}</span></div><footer><button class="quiet-button" :disabled="busy" @click="reviseMemory(memory)">修改</button><button class="quiet-button" :disabled="busy || memory.status !== 'active'" @click="memoryAction(memory, 'expire')">过期</button><button class="text-danger" :disabled="busy || memory.status === 'retracted'" @click="memoryAction(memory, 'retract')">撤回</button><button class="quiet-button" :disabled="busy" @click="memoryAction(memory, 'undo')">撤销最近操作</button></footer></section><p v-if="!personDetail.memories.length" class="empty-inline">从事件层重算，或添加一条带证据的人物记忆。</p></div></article>

          <div class="person-memory-grid bottom-grid">
            <article class="panel memory-section"><header><div><p class="section-kicker">OPEN COMMITMENTS</p><h3>未完成承诺</h3></div></header><div class="compact-memory-list"><p v-for="memory in personDetail.commitments" :key="memory.memory_id"><strong>{{ memory.summary }}</strong><small>{{ memory.details.commitment_direction || '方向待确认' }}<template v-if="memory.reminder"> · 已关联提醒 {{ formatDate(memory.reminder.scheduled_at) }}</template></small></p><span v-if="!personDetail.commitments.length">没有有效的未完成承诺</span></div></article>
            <article class="panel memory-section"><header><div><p class="section-kicker">INTERACTION TIMELINE</p><h3>跨天互动</h3></div></header><div class="interaction-list"><p v-for="interaction in personDetail.interactions" :key="`${interaction.interaction_type}-${interaction.event_id ?? interaction.session_id}`"><time>{{ formatDate(interaction.occurred_at) }}</time><strong>{{ interaction.title }}</strong><small>{{ interaction.interaction_type === 'event' ? interaction.event_kind : `${interaction.utterance_count ?? 0} 条原话` }} · 会话 {{ interaction.session_id.slice(0, 8) }}</small></p></div></article>
          </div>
        </section>
      </div>
    </PageState>

    <PageState v-else :loading="query.loading.value" :error="query.error.value" :empty="!query.data.value?.clusters.length" empty-text="还没有说话人簇。选择一段已处理录音开始本机分析。" @retry="query.refresh(true)">
      <div class="people-layout">
        <aside class="panel cluster-list">
          <button v-for="cluster in query.data.value?.clusters" :key="cluster.cluster_id" :class="{ active: selectedId === cluster.cluster_id }" @click="selectCluster(cluster.cluster_id)"><span class="speaker-mark">{{ cluster.person_name?.slice(0, 1) ?? '?' }}</span><span><strong>{{ cluster.person_name ?? cluster.display_label }}</strong><small>{{ cluster.session_count }} 段录音 · {{ cluster.track_count }} 条说话轨</small><small v-if="cluster.suggested_person_id">可能匹配 · {{ Math.round((cluster.suggestion_confidence ?? 0) * 100) }}%</small></span><i>{{ cluster.status }}</i></button>
        </aside>
        <section v-if="detail" class="cluster-workspace">
          <article class="panel cluster-hero"><header><div><p class="section-kicker">CLUSTER {{ detail.cluster_id.slice(0, 8) }}</p><h2>{{ detail.person_name ?? detail.display_label }}</h2></div><span class="status-pill">{{ detail.person_name ? '已确认' : detail.suggested_person_id ? '可能匹配' : '暂无法判断' }}</span></header><div class="cluster-metrics"><span><small>跨录音</small><strong>{{ detail.session_count }}</strong></span><span><small>说话轨</small><strong>{{ detail.track_count }}</strong></span><span><small>候选原型</small><strong>{{ detail.prototypes?.filter((item) => item.status === 'candidate').length ?? 0 }}</strong></span><span><small>版本</small><strong>r{{ detail.revision }}</strong></span></div><div class="representative-clips"><small>代表片段</small><button v-for="clip in detail.prototypes?.flatMap((item) => item.representative_clips).slice(0, 5)" :key="`${clip.media_id}-${clip.start_ms}`" class="quiet-button" @click="playRange(clip.media_id, clip.start_ms, clip.end_ms)">▶ {{ Math.round((clip.end_ms - clip.start_ms) / 1000) }} 秒</button><span v-if="!detail.prototypes?.length">暂无可播放片段</span></div></article>
          <div class="cluster-actions-grid">
            <article class="panel identity-action"><h3>确认人物</h3><p>确认后才复制多个候选原型到稳定声纹库；不会压成单一平均值。</p><label><span>已有联系人</span><select v-model="selectedPerson"><option value="">选择人物</option><option v-for="person in query.data.value?.people" :key="person.person_id" :value="person.person_id">{{ person.display_name }} · {{ person.prototype_count }} 原型</option></select></label><button class="primary-action" :disabled="!selectedPerson || busy" @click="labelExisting">关联已有</button><label><span>或新建人物</span><input v-model="newPersonName" placeholder="姓名或称呼" /></label><button class="quiet-button" :disabled="!newPersonName.trim() || busy" @click="createAndLabel">新建并关联</button></article>
            <article class="panel identity-action"><h3>纠正聚类</h3><p>合并和拆分只移动匿名说话轨，原始识别产物保持不变。</p><label><span>合并另一个未知簇</span><select v-model="mergeSource"><option value="">选择来源簇</option><option v-for="cluster in mergeChoices" :key="cluster.cluster_id" :value="cluster.cluster_id">{{ cluster.person_name ?? cluster.display_label }}</option></select></label><button class="quiet-button" :disabled="!mergeSource || busy" @click="merge">合并到当前簇</button><fieldset><legend>拆出说话轨</legend><label v-for="member in detail.members" :key="member.speaker_track_id" class="track-check"><input v-model="splitTracks" type="checkbox" :value="member.speaker_track_id" />{{ member.label }} · {{ member.session_id.slice(0, 8) }}</label></fieldset><button class="quiet-button" :disabled="!splitTracks.length || splitTracks.length === detail.members?.length || busy" @click="split">拆成新簇</button></article>
            <article class="panel identity-action safety-action"><h3>保留未知或排除</h3><p>没有足够证据时无需操作，系统会继续保留匿名簇。电视、广播和环境声可以明确排除。</p><button class="quiet-button" disabled>暂无法判断（保持未知）</button><button class="quiet-button" :disabled="busy" @click="ignore">忽略媒体 / 环境声</button><button class="text-danger" :disabled="busy" @click="undo">撤销最近操作</button></article>
          </div>
        </section>
      </div>
    </PageState>
  </section>
</template>

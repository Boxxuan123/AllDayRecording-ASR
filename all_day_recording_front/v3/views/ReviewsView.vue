<script setup lang="ts">
import { computed, ref } from 'vue'

import PageState from '../components/PageState.vue'
import VoiceReviewActions from '../components/VoiceReviewActions.vue'
import { desktopApi } from '../core/api'
import { formatDate } from '../core/format'
import { playRange } from '../core/media'
import { useQuery } from '../core/query'
import {
  reviewCategories,
  reviewCategory,
  reviewCategoryLabels as categoryLabels,
  reviewDecisionReason,
  reviewKindLabels as kindLabels,
  voiceReviewLane,
  type ReviewCategory,
  type VoiceReviewLane,
} from '../core/reviews'
import { navigate } from '../core/router'
import type {
  PersonMemory,
  ReminderCandidate,
  ReviewItem,
  SpeakerMatchTier,
  VoicePrototypeCandidate,
  VoicePrototypeReviewStatus,
} from '../core/types'

type ReviewFilter = 'all' | ReviewCategory
type ReviewGroupMode = 'session' | 'person'
type ReviewUnit = ReviewItem & { members: ReviewItem[] }

const query = useQuery('reviews', async () => {
  const reviews = await desktopApi.reviews()
  const needsReminders = reviews.items.some((item) => item.kind === 'reminder')
  const [voiceCandidates, reminderCandidates] = await Promise.all([
    desktopApi.voicePrototypeCandidates().catch(() => ({ items: [] as VoicePrototypeCandidate[] })),
    needsReminders
      ? desktopApi.reminderCandidates().catch(() => ({ items: [] as ReminderCandidate[] }))
      : Promise.resolve({ items: [] as ReminderCandidate[] }),
  ])
  return {
    items: reviews.items,
    voiceCandidates: voiceCandidates.items,
    reminderCandidates: reminderCandidates.items,
  }
})

const selectedKind = ref<ReviewFilter>('all')
const groupMode = ref<ReviewGroupMode>('session')
const busyId = ref('')
const actionError = ref('')
const notice = ref('')

const tierLabels: Record<SpeakerMatchTier, string> = {
  insufficient_evidence: '证据不足',
  auto_matched: '已自动匹配',
  suggested: '建议核对',
  no_known_match: '未达阈值',
}

const voiceCandidates = computed(() => new Map(
  (query.data.value?.voiceCandidates ?? []).map((item) => [item.prototype_id, item]),
))

const reminderCandidates = computed(() => new Map(
  (query.data.value?.reminderCandidates ?? []).map((item) => [item.candidate_id, item]),
))

const reviewUnits = computed<ReviewUnit[]>(() => {
  const units = new Map<string, ReviewUnit>()
  for (const item of query.data.value?.items ?? []) {
    // Backward compatibility with a local server that has not restarted yet.
    if (item.kind === 'workflow_failure') continue
    if (item.kind !== 'voice_identity') {
      units.set(item.review_id, { ...item, members: [item] })
      continue
    }
    const candidate = voiceCandidates.value.get(item.source_id)
    const declaredLane = item.context.review_lane
    const lane = declaredLane === 'primary' || declaredLane === 'training'
      ? declaredLane
      : candidate ? voiceReviewLane(candidate) : item.context.voice_mode === 'speaker_discovery' ? 'primary' : null
    if (!lane) continue
    const clusterId = stringValue(item.context.cluster_id) ?? candidate?.cluster_id ?? item.source_id
    const key = `voice:${clusterId}:${item.person_id ?? 'unknown'}:${lane}`
    const prototypeIds = unique([
      ...stringArray(item.context.prototype_ids),
      ...(candidate ? [candidate.prototype_id] : []),
    ])
    const sessionIds = unique([
      ...stringArray(item.context.session_ids),
      ...(item.session_id ? [item.session_id] : []),
      ...(candidate ? [candidate.session_id] : []),
    ])
    const current = units.get(key)
    if (current) {
      current.members.push(item)
      current.evidence_count += item.evidence_count
      current.created_at = current.created_at < item.created_at ? current.created_at : item.created_at
      current.updated_at = current.updated_at > item.updated_at ? current.updated_at : item.updated_at
      current.session_id = current.session_id === item.session_id ? current.session_id : null
      current.context.prototype_ids = unique([...stringArray(current.context.prototype_ids), ...prototypeIds])
      current.context.session_ids = unique([...stringArray(current.context.session_ids), ...sessionIds])
      current.context.candidate_count = stringArray(current.context.prototype_ids).length
      continue
    }
    units.set(key, {
      ...item,
      review_id: key,
      source_id: clusterId,
      session_id: sessionIds.length === 1 ? sessionIds[0] : null,
      context: {
        ...item.context,
        cluster_id: clusterId,
        review_lane: lane,
        prototype_ids: prototypeIds,
        session_ids: sessionIds,
        candidate_count: prototypeIds.length || Number(item.context.candidate_count ?? 0),
      },
      members: [item],
    })
  }
  return [...units.values()]
})

const primaryItems = computed(() => reviewUnits.value.filter((item) => reviewLane(item) !== 'training'))
const counts = computed(() => Object.fromEntries(
  reviewCategories.map((category) => [
    category,
    primaryItems.value.filter((item) => reviewCategory(item) === category).length,
  ]),
) as Record<ReviewCategory, number>)
const activeCategories = computed(() => reviewCategories.filter((category) => counts.value[category] > 0))
const effectiveKind = computed<ReviewFilter>(() => selectedKind.value === 'all' || counts.value[selectedKind.value] > 0
  ? selectedKind.value
  : 'all')
const filteredItems = computed(() => effectiveKind.value === 'all'
  ? primaryItems.value
  : primaryItems.value.filter((item) => reviewCategory(item) === effectiveKind.value))

const groupedItems = computed(() => {
  const groups = new Map<string, { key: string; label: string; items: ReviewUnit[] }>()
  for (const item of filteredItems.value) {
    const personGroup = item.kind === 'voice_identity' || item.kind === 'person_memory'
    const sessions = stringArray(item.context.session_ids)
    const key = groupMode.value === 'person'
      ? personGroup && item.person_id ? `person:${item.person_id}` : `kind:${item.kind}`
      : item.session_id ? `session:${item.session_id}` : sessions.length > 1 ? `multi:${item.review_id}` : `kind:${item.kind}`
    const label = groupMode.value === 'person'
      ? personGroup && item.person_id ? `人物 · ${item.title}` : kindLabels[item.kind]
      : item.session_id ? `录音 · ${item.session_id.slice(-10)}` : sessions.length > 1 ? `跨 ${sessions.length} 次录音` : kindLabels[item.kind]
    const current = groups.get(key)
    if (current) current.items.push(item)
    else groups.set(key, { key, label, items: [item] })
  }
  return [...groups.values()]
})

function setKindFilter(kind: ReviewCategory): void {
  selectedKind.value = selectedKind.value === kind ? 'all' : kind
}

function reviewLane(item: ReviewItem): VoiceReviewLane | null {
  return item.context.review_lane === 'primary' || item.context.review_lane === 'training'
    ? item.context.review_lane
    : null
}

function voiceMode(item: ReviewItem): 'known_person' | 'speaker_discovery' {
  return item.context.voice_mode === 'speaker_discovery' ? 'speaker_discovery' : 'known_person'
}

function candidatesFor(item: ReviewItem): VoicePrototypeCandidate[] {
  const ids = stringArray(item.context.prototype_ids)
  if (!ids.length && voiceCandidates.value.has(item.source_id)) ids.push(item.source_id)
  return ids.map((id) => voiceCandidates.value.get(id)).filter((value): value is VoicePrototypeCandidate => Boolean(value))
}

function reminderCandidate(item: ReviewItem): ReminderCandidate | undefined {
  return reminderCandidates.value.get(item.source_id)
}

function voiceTier(item: ReviewItem): SpeakerMatchTier | null {
  const value = candidatesFor(item)[0]?.decision_tier ?? item.context.decision_tier
  return typeof value === 'string' && value in tierLabels ? value as SpeakerMatchTier : null
}

function voiceTierLabel(item: ReviewItem): string {
  if (voiceMode(item) === 'speaker_discovery') return '新人物候选'
  const tier = voiceTier(item)
  return tier ? tierLabels[tier] : '等待核对'
}

function percentage(value: unknown): string | null {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return null
  return `${Math.round(Number(value) * 100)}%`
}

function percentRange(values: Array<number | null | undefined>): string | null {
  const numbers = values.filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  if (!numbers.length) return null
  const minimum = Math.min(...numbers)
  const maximum = Math.max(...numbers)
  return minimum === maximum ? percentage(minimum) : `${percentage(minimum)}–${percentage(maximum)}`
}

function cardTitle(item: ReviewItem): string {
  if (item.kind === 'processing_gate') return `是否允许这项高风险处理：“${item.title}”？`
  if (item.kind === 'reminder') {
    const operation = String(item.context.operation ?? '')
    if (operation === 'UPDATE_EVENT') return `是否修改提醒：“${item.title}”？`
    if (operation === 'CANCEL_EVENT') return `是否取消提醒：“${item.title}”？`
    if (operation === 'MARK_DONE') return `是否将提醒标记完成：“${item.title}”？`
    return `是否创建提醒：“${item.title}”？`
  }
  if (item.kind === 'person_memory') return `是否长期记住：“${item.summary}”？`
  if (item.kind === 'knowledge_proposal') {
    return item.context.event_kind === 'person_fact'
      ? `是否将“${item.summary}”作为人物事实？`
      : `是否确认这项决定：“${item.summary}”？`
  }
  if (item.kind === 'voice_identity' && voiceMode(item) === 'speaker_discovery') return item.title
  if (item.kind === 'voice_identity') {
    const count = candidatesFor(item).length || Number(item.context.candidate_count ?? 1)
    return count > 1 ? `这组声音是否属于 ${item.title}` : `这段声音是否属于 ${item.title}`
  }
  return item.title
}

function cardSummary(item: ReviewItem): string {
  if (item.kind === 'processing_gate') {
    return item.context.error ? String(item.context.error) : '处理流程已暂停，等待人工判断。'
  }
  if (item.kind === 'reminder') {
    const schedule = item.context.scheduled_at ? formatDate(String(item.context.scheduled_at)) : '时间未确定'
    const location = item.context.location ? ` · ${item.context.location}` : ''
    const confidence = percentage(item.context.confidence)
    return `${schedule}${location}${confidence ? ` · 模型置信 ${confidence}` : ''}`
  }
  if (item.kind === 'voice_identity' && voiceMode(item) === 'speaker_discovery') {
    return `${item.summary} · ${Number(item.context.track_count ?? item.evidence_count)} 条声音轨`
  }
  if (item.kind === 'voice_identity') {
    const candidates = candidatesFor(item)
    const fallbackScores = [numberValue(item.context.minimum_score), numberValue(item.context.best_score)]
    const score = percentRange(candidates.map((candidate) => candidate.best_score)) ?? percentRange(fallbackScores)
    const quality = percentRange(candidates.map((candidate) => candidate.quality_score)) ?? percentage(item.context.quality_score)
    const sessions = unique(candidates.map((candidate) => candidate.session_id).concat(stringArray(item.context.session_ids)))
    return [
      score ? `匹配度 ${score}` : '暂无匹配分',
      quality ? `音质 ${quality}` : null,
      `${candidates.length || Number(item.context.candidate_count ?? 1)} 个样本`,
      sessions.length ? `${sessions.length} 次录音` : null,
    ].filter(Boolean).join(' · ')
  }
  if (item.kind === 'knowledge_proposal') {
    const confidence = percentage(item.context.confidence)
    return `${item.summary}${confidence ? ` · 模型置信 ${confidence}` : ''}`
  }
  const confidence = percentage(item.context.confidence)
  return confidence ? `${confidence} 可信度 · ${item.evidence_count} 条证据` : `${item.evidence_count} 条证据`
}

function targetFor(item: ReviewItem): string {
  if (item.kind === 'processing_gate' && item.session_id) return `/recordings/${encodeURIComponent(item.session_id)}?tab=runs`
  if (item.kind === 'reminder') return `/reminders?candidate=${encodeURIComponent(item.source_id)}`
  if (item.kind === 'person_memory' && item.person_id) {
    return `/people?mode=memory&person=${encodeURIComponent(item.person_id)}&memory=${encodeURIComponent(item.source_id)}`
  }
  if (item.kind === 'voice_identity' && voiceMode(item) === 'speaker_discovery') {
    return `/people?mode=voice&cluster=${encodeURIComponent(item.source_id)}`
  }
  if (item.kind === 'voice_identity' && item.person_id) {
    const prototypeId = candidatesFor(item)[0]?.prototype_id
    return `/people?mode=voice&person=${encodeURIComponent(item.person_id)}${prototypeId ? `&prototype=${encodeURIComponent(prototypeId)}` : ''}`
  }
  if (item.kind === 'knowledge_proposal' && item.session_id) {
    return `/recordings/${encodeURIComponent(item.session_id)}?tab=timeline`
  }
  return '/reviews'
}

async function act(item: ReviewItem, action: () => Promise<unknown>, message: string, actionId = item.review_id): Promise<void> {
  busyId.value = actionId
  actionError.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = message
    await query.refresh(true)
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    busyId.value = ''
  }
}

function playReminder(item: ReviewItem): void {
  const evidence = reminderCandidate(item)?.evidence[0]
  if (evidence) playRange(evidence.media_id, evidence.asset_start_ms, evidence.asset_end_ms)
}

function reviewVoiceCandidate(
  item: ReviewItem,
  candidate: VoicePrototypeCandidate,
  decision: Exclude<VoicePrototypeReviewStatus, 'pending' | 'retracted'>,
): void {
  if (!item.person_id) return
  const message = decision === 'confirmed'
    ? `已确认这个声音样本属于 ${item.title}。其余样本没有被批量写入。`
    : decision === 'rejected' ? `已记录这个样本不属于 ${item.title}。其余样本不受影响。` : '这个样本已保留为待定，不会进入稳定声纹库。'
  void act(
    item,
    () => desktopApi.reviewVoicePrototype(candidate.prototype_id, item.person_id!, decision),
    message,
    `${item.review_id}:${candidate.prototype_id}`,
  )
}

function confirmReminder(item: ReviewItem): void {
  void act(item, () => desktopApi.confirmReminder(item.source_id), `已确认提醒“${item.title}”。`)
}

function ignoreReminder(item: ReviewItem): void {
  void act(item, () => desktopApi.ignoreReminder(item.source_id, 'desktop_user_ignored_from_review_inbox'), `已忽略提醒“${item.title}”。`)
}

async function currentMemory(item: ReviewItem): Promise<PersonMemory> {
  if (!item.person_id) throw new Error('这条记忆没有关联人物。')
  const person = await desktopApi.person(item.person_id)
  const memory = [...person.memories, ...person.commitments].find((candidate) => candidate.memory_id === item.source_id)
  if (!memory) throw new Error('找不到这条人物记忆，可能已经在其他页面处理。')
  return memory
}

function confirmMemory(item: ReviewItem): void {
  void act(item, async () => {
    const memory = await currentMemory(item)
    return desktopApi.revisePersonMemory(memory.memory_id, {
      summary: memory.summary,
      details: memory.details,
      confidence: memory.confidence,
      valid_from: memory.valid_from,
      valid_until: memory.valid_until,
      reminder_event_id: memory.reminder_event_id,
    })
  }, '人物记忆已确认，原版本和证据仍可审计。')
}

function retractMemory(item: ReviewItem): void {
  void act(item, () => desktopApi.retractPersonMemory(item.source_id), '未采用这条记忆；历史版本和证据仍可审计。')
}

function acceptProposal(item: ReviewItem): void {
  void act(item, () => desktopApi.acceptKnowledgeProposal(item.source_id), `已确认“${item.title}”。`)
}

function rejectProposal(item: ReviewItem): void {
  void act(
    item,
    () => desktopApi.rejectKnowledgeProposal(item.source_id, 'desktop_user_did_not_adopt_from_review_inbox'),
    `未采用“${item.title}”。`,
  )
}

function createDiscoveredPerson(item: ReviewItem): void {
  const name = window.prompt('给这位人物一个名称')?.trim()
  if (!name) return
  void act(item, () => desktopApi.createAndLabelSpeaker(item.source_id, name), `已创建人物“${name}”并关联这组声音。`)
}

function ignoreDiscoveredSpeaker(item: ReviewItem): void {
  if (!window.confirm('忽略后，这组未知声音将不再作为新人物候选。继续吗？')) return
  void act(item, () => desktopApi.ignoreSpeakerCluster(item.source_id, 'desktop_user_ignored_recurring_unknown'), '已忽略这组未知声音。')
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function unique(values: string[]): string[] {
  return [...new Set(values)]
}
</script>

<template>
  <section class="page review-page">
    <header class="page-header">
      <div><p class="overline">REVIEW INBOX</p><h1>审核收件箱</h1></div>
      <span class="header-note">这里只放需要你判断、且判断后会改变长期状态的事项</span>
    </header>
    <PageState
      :loading="query.loading.value"
      :error="query.error.value"
      :empty="!primaryItems.length"
      empty-text="当前没有需要你决定的事项。明确指令会直接执行，普通推断由系统自行维护。"
      @retry="query.refresh(true)"
    >
      <div v-if="activeCategories.length > 1" class="review-summary panel" aria-label="按审核类型筛选">
        <button
          v-for="kind in activeCategories"
          :key="kind"
          type="button"
          :class="{ active: effectiveKind === kind }"
          :data-kind="kind"
          :aria-pressed="effectiveKind === kind"
          :title="effectiveKind === kind ? '再次点击显示全部审核' : `只看${categoryLabels[kind]}`"
          @click="setKindFilter(kind)"
        >
          <small>{{ categoryLabels[kind] }}</small><strong>{{ counts[kind] }}</strong>
        </button>
      </div>
      <div class="review-toolbar panel-flat">
        <div>
          <strong>{{ effectiveKind === 'all' ? '需要决定' : categoryLabels[effectiveKind] }}</strong>
          <span>显示 {{ filteredItems.length }} / {{ primaryItems.length }} 个任务</span>
          <button v-if="effectiveKind !== 'all'" type="button" class="text-button" @click="selectedKind = 'all'">查看全部</button>
        </div>
        <div class="review-group-switch" aria-label="审核分组方式">
          <span>分组</span>
          <button type="button" :class="{ active: groupMode === 'session' }" :aria-pressed="groupMode === 'session'" @click="groupMode = 'session'">按录音</button>
          <button type="button" :class="{ active: groupMode === 'person' }" :aria-pressed="groupMode === 'person'" @click="groupMode = 'person'">按人物</button>
        </div>
      </div>
      <p v-if="actionError" class="review-message error-state">{{ actionError }}</p>
      <p v-else-if="notice" class="review-message">{{ notice }}</p>
      <div v-if="!filteredItems.length" class="state-panel">当前筛选下没有待审核项目。</div>
      <div v-else class="review-groups">
        <section v-for="group in groupedItems" :key="group.key" class="panel review-group">
          <header>
            <div><p class="section-kicker">{{ groupMode === 'session' ? 'SESSION QUEUE' : 'PERSON QUEUE' }}</p><h2>{{ group.label }}</h2></div>
            <span>{{ group.items.length }} 个任务</span>
          </header>
          <div class="review-list">
            <article v-for="item in group.items" :key="item.review_id" class="review-row" :data-kind="item.kind" :data-priority="item.priority">
              <div class="review-row-copy">
                <div class="review-badges">
                  <span class="review-kind">{{ kindLabels[item.kind] }}</span>
                  <span v-if="item.kind === 'voice_identity'" class="review-tier" :data-tier="voiceTier(item)">{{ voiceTierLabel(item) }}</span>
                  <span v-if="item.priority === 'high'" class="review-priority">优先处理</span>
                </div>
                <h3>{{ cardTitle(item) }}</h3>
                <p class="review-decision-reason">{{ reviewDecisionReason(item) }}</p>
                <p class="review-row-summary">{{ cardSummary(item) }}</p>
                <div class="review-row-meta">
                  <span>{{ item.evidence_count ? `${item.evidence_count} 条证据` : '流程状态' }}</span>
                  <span>进入队列 {{ formatDate(item.created_at) }}</span>
                </div>
              </div>
              <div class="review-row-actions">
                <template v-if="item.kind === 'voice_identity' && voiceMode(item) === 'speaker_discovery'">
                  <button type="button" class="review-action reject" :disabled="busyId === item.review_id" @click="ignoreDiscoveredSpeaker(item)">忽略</button>
                  <button type="button" class="review-action" @click="navigate(targetFor(item))">合并到已有声音</button>
                  <button type="button" class="review-action primary" :disabled="busyId === item.review_id" @click="createDiscoveredPerson(item)">创建人物</button>
                </template>
                <template v-else-if="item.kind === 'voice_identity'">
                  <VoiceReviewActions
                    :candidates="candidatesFor(item)"
                    :busy-id="busyId"
                    :review-id="item.review_id"
                    @review="(candidate, decision) => reviewVoiceCandidate(item, candidate, decision)"
                  />
                </template>
                <template v-else-if="item.kind === 'reminder'">
                  <button type="button" class="review-action play" :disabled="busyId === item.review_id || !reminderCandidate(item)?.evidence.length" @click="playReminder(item)">▶ 原话</button>
                  <button type="button" class="review-action reject" :disabled="busyId === item.review_id" @click="ignoreReminder(item)">不执行</button>
                  <button type="button" class="review-action primary" :disabled="busyId === item.review_id" @click="confirmReminder(item)">{{ busyId === item.review_id ? '处理中' : '确认执行' }}</button>
                </template>
                <template v-else-if="item.kind === 'person_memory'">
                  <button type="button" class="review-action reject" :disabled="busyId === item.review_id" @click="retractMemory(item)">不记</button>
                  <button type="button" class="review-action primary" :disabled="busyId === item.review_id" @click="confirmMemory(item)">{{ busyId === item.review_id ? '处理中' : '记住' }}</button>
                </template>
                <template v-else-if="item.kind === 'knowledge_proposal'">
                  <button type="button" class="review-action reject" :disabled="busyId === item.review_id" @click="rejectProposal(item)">不采用</button>
                  <button type="button" class="review-action primary" :disabled="busyId === item.review_id" @click="acceptProposal(item)">{{ busyId === item.review_id ? '处理中' : '确认' }}</button>
                </template>
                <button type="button" class="review-action detail" @click="navigate(targetFor(item))">查看证据 →</button>
              </div>
            </article>
          </div>
        </section>
      </div>
    </PageState>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watchEffect } from 'vue'

import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatDate } from '../core/format'
import { playRange, release } from '../core/media'
import { useQuery } from '../core/query'
import type { ReminderCandidate, ReminderCandidateStatus } from '../core/types'

const query = useQuery('reminders', async () => {
  const [candidates, reminders, sessions] = await Promise.all([
    desktopApi.reminderCandidates(),
    desktopApi.reminders(),
    desktopApi.sessions(),
  ])
  return { candidates: candidates.items, reminders: reminders.items, sessions: sessions.items }
})
const focusedCandidateId = new URLSearchParams(window.location.search).get('candidate') ?? ''
const filter = ref<'pending' | 'all' | 'resolved'>('pending')
const filterOptions: { value: 'pending' | 'all' | 'resolved'; label: string }[] = [
  { value: 'pending', label: '待确认' },
  { value: 'all', label: '全部' },
  { value: 'resolved', label: '已处理' },
]
const busy = ref<string | null>(null)
const actionError = ref('')
const editing = ref<ReminderCandidate | null>(null)
const draftTitle = ref('')
const draftTime = ref('')
const draftLocation = ref('')
const selectedSessionId = ref('')
const reasoningEffort = ref<'auto' | 'low' | 'medium' | 'high' | 'xhigh'>('auto')
const generating = ref(false)
const generationMessage = ref('')
let focusedDeepLink = false

watchEffect(() => {
  if (!selectedSessionId.value && query.data.value?.sessions.length) {
    selectedSessionId.value = query.data.value.sessions[0].session_id
  }
  if (!focusedDeepLink && query.data.value?.candidates.some((item) => item.candidate_id === focusedCandidateId)) {
    focusedDeepLink = true
    void nextTick(() => document.getElementById(`reminder-candidate-${focusedCandidateId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
  }
})

const candidates = computed(() => {
  const values = query.data.value?.candidates ?? []
  if (filter.value === 'pending') return values.filter((item) => item.status === 'pending_confirmation')
  if (filter.value === 'resolved') return values.filter((item) => item.status !== 'pending_confirmation')
  return values
})

const statusNames: Record<ReminderCandidateStatus, string> = {
  pending_confirmation: '等待确认',
  auto_applied: '安全策略已创建',
  confirmed: '已确认',
  modified: '已修改',
  ignored: '已忽略',
  duplicate: '重复候选',
  conflict: '版本冲突',
}

const operationNames: Record<ReminderCandidate['operation'], string> = {
  CREATE_TASK: '新建待办',
  CREATE_APPOINTMENT: '新建约定',
  UPDATE_EVENT: '修改提醒',
  CANCEL_EVENT: '取消提醒',
  MARK_DONE: '标记完成',
  IGNORE: '忽略',
}

function directionName(value: ReminderCandidate['commitment_direction']): string {
  return {
    self_to_other: '我答应别人',
    other_to_self: '别人答应我',
    mutual: '双方约定',
    not_applicable: '不适用',
  }[value]
}

function playEvidence(item: ReminderCandidate): void {
  const evidence = item.evidence[0]
  if (!evidence) return
  playRange(evidence.media_id, evidence.asset_start_ms, evidence.asset_end_ms)
}

async function act(candidateId: string, action: () => Promise<unknown>): Promise<void> {
  busy.value = candidateId
  actionError.value = ''
  try {
    await action()
    await query.refresh(true)
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    busy.value = null
  }
}

function openEdit(item: ReminderCandidate): void {
  editing.value = item
  draftTitle.value = item.title ?? ''
  draftTime.value = item.scheduled_at
    ? new Date(item.scheduled_at).toISOString().slice(0, 16)
    : ''
  draftLocation.value = item.location ?? ''
}

async function saveEdit(): Promise<void> {
  if (!editing.value || !draftTitle.value.trim() || !draftTime.value) return
  const item = editing.value
  await act(item.candidate_id, () => desktopApi.modifyReminder(item.candidate_id, {
    title: draftTitle.value.trim(),
    scheduled_at: new Date(draftTime.value).toISOString(),
    location: draftLocation.value.trim() || null,
  }))
  if (!actionError.value) editing.value = null
}

function ignore(item: ReminderCandidate): void {
  if (!window.confirm(`忽略“${item.title ?? operationNames[item.operation]}”候选？`)) return
  void act(item.candidate_id, () => desktopApi.ignoreReminder(item.candidate_id, 'desktop_user_ignored'))
}

async function generateWithCodex(): Promise<void> {
  if (!selectedSessionId.value || generating.value) return
  generating.value = true
  actionError.value = ''
  generationMessage.value = ''
  try {
    const result = await desktopApi.generateRemindersWithCodex(
      selectedSessionId.value,
      reasoningEffort.value,
    )
    generationMessage.value = result.candidates.length
      ? `Codex 使用 ${result.codex.reasoning_effort} 推理生成 ${result.candidates.length} 个候选。`
      : `Codex 使用 ${result.codex.reasoning_effort} 推理完成分析，没有发现证据充分的提醒。`
    await query.refresh(true)
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    generating.value = false
  }
}

onBeforeUnmount(release)
</script>

<template>
  <section class="page reminders-page">
    <header class="page-header">
      <div><p class="overline">INTELLIGENT REMINDER LOOP</p><h1>候选<em>提醒</em></h1></div>
      <span class="header-note">模型只提交候选；确认、修改、忽略均留下证据和反馈</span>
    </header>

    <div class="reminder-summary">
      <article><small>等待判断</small><strong>{{ query.data.value?.candidates.filter((item) => item.status === 'pending_confirmation').length ?? 0 }}</strong></article>
      <article><small>有效提醒</small><strong>{{ query.data.value?.reminders.filter((item) => item.status === 'scheduled' || item.status === 'delivered').length ?? 0 }}</strong></article>
      <article><small>闭环完成</small><strong>{{ query.data.value?.reminders.filter((item) => item.status === 'completed' || item.status === 'cancelled').length ?? 0 }}</strong></article>
    </div>

    <section class="panel codex-reminder-runner">
      <div>
        <p class="section-kicker">CODEX EXTRACTION</p>
        <h2>从一段对话生成候选</h2>
        <p>仅发送必要转写字段；空工作目录保持只读，模型只能返回结构化候选。</p>
      </div>
      <label><span>录音会话</span><select v-model="selectedSessionId">
        <option value="" disabled>选择会话</option>
        <option v-for="session in query.data.value?.sessions" :key="session.session_id" :value="session.session_id">{{ formatDate(session.captured_start) }} · {{ session.session_id.slice(0, 8) }}</option>
      </select></label>
      <label><span>推理强度</span><select v-model="reasoningEffort">
        <option value="auto">按需自动</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="xhigh">XHigh</option>
      </select></label>
      <button class="primary-action" :disabled="!selectedSessionId || generating" @click="generateWithCodex">{{ generating ? 'Codex 分析中…' : '生成候选' }}</button>
    </section>
    <p v-if="generationMessage" class="generation-message">{{ generationMessage }}</p>

    <div class="filter-tabs reminder-filters" aria-label="候选状态筛选">
      <button v-for="item in filterOptions" :key="item.value" :class="{ active: filter === item.value }" @click="filter = item.value">{{ item.label }}</button>
    </div>

    <p v-if="actionError" class="form-error reminder-error">{{ actionError }}</p>
    <PageState :loading="query.loading.value" :error="query.error.value" :empty="!candidates.length" empty-text="当前筛选下没有候选提醒。" @retry="query.refresh(true)">
      <div class="reminder-layout">
        <section class="reminder-candidate-list">
          <article v-for="item in candidates" :id="`reminder-candidate-${item.candidate_id}`" :key="item.candidate_id" :class="['panel', 'reminder-card', item.candidate_id === focusedCandidateId ? 'focused-review' : '']" :data-status="item.status">
            <header>
              <div><span class="reminder-operation">{{ operationNames[item.operation] }}</span><h2>{{ item.title ?? '无标题操作' }}</h2></div>
              <span class="status-pill">{{ statusNames[item.status] }}</span>
            </header>
            <div class="reminder-meta">
              <span><small>时间</small><strong>{{ item.scheduled_at ? formatDate(item.scheduled_at) : '沿用原事件' }}</strong></span>
              <span><small>行动关系</small><strong>{{ directionName(item.commitment_direction) }}</strong></span>
              <span><small>地点</small><strong>{{ item.location || '未指定' }}</strong></span>
              <span><small>模型置信</small><strong>{{ Math.round(item.confidence * 100) }}%</strong></span>
            </div>
            <div class="reminder-evidence">
              <button class="play-dot" :disabled="!item.evidence.length" aria-label="播放候选原话" @click="playEvidence(item)">▶</button>
              <div><small>原话证据</small><p>{{ item.evidence[0]?.text ?? '证据索引暂不可用' }}</p><code>{{ item.evidence_utterance_ids.join(' · ') }}</code></div>
            </div>
            <p v-if="item.conflict_reason" class="candidate-reason">{{ item.conflict_reason }}</p>
            <footer v-if="item.status === 'pending_confirmation'">
              <button class="quiet-button" :disabled="busy === item.candidate_id" @click="ignore(item)">忽略</button>
              <button class="quiet-button" :disabled="busy === item.candidate_id" @click="openEdit(item)">修改</button>
              <button class="primary-action" :disabled="busy === item.candidate_id" @click="act(item.candidate_id, () => desktopApi.confirmReminder(item.candidate_id))">{{ busy === item.candidate_id ? '处理中' : '确认提醒' }}</button>
            </footer>
          </article>
        </section>

        <aside class="panel active-reminders">
          <header><div><p class="section-kicker">EXECUTABLE QUEUE</p><h2>本地提醒队列</h2></div></header>
          <article v-for="item in query.data.value?.reminders" :key="item.event_id">
            <span class="status-pill">{{ item.status }}</span>
            <strong>{{ item.title }}</strong>
            <small>{{ formatDate(item.scheduled_at) }} · revision {{ item.event_revision }}</small>
          </article>
          <p v-if="!query.data.value?.reminders.length" class="empty-inline">还没有已确认提醒。</p>
        </aside>
      </div>
    </PageState>

    <div v-if="editing" class="dialog-backdrop" @click.self="editing = null">
      <form class="dialog" @submit.prevent="saveEdit">
        <h2>修改候选提醒</h2>
        <p>修改会生成新的人工 proposal；原候选保留为已修改，不覆盖模型原始输出。</p>
        <label><span>标题</span><input v-model="draftTitle" required /></label>
        <label><span>提醒时间</span><input v-model="draftTime" type="datetime-local" required /></label>
        <label><span>地点</span><input v-model="draftLocation" /></label>
        <div><button type="button" class="quiet-button" @click="editing = null">取消</button><button class="primary-action" :disabled="busy === editing.candidate_id">保存并确认</button></div>
      </form>
    </div>
  </section>
</template>

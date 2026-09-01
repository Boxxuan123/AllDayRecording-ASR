<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watchEffect } from 'vue'

import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { playRange, release } from '../core/media'
import { useQuery } from '../core/query'
import type { InsightEvidence, RelationshipObservation } from '../core/types'

const timezone = ref('Asia/Singapore')
const selectedDate = ref(new Date().toISOString().slice(0, 10))
const selectedPersonId = ref('')
const windowDays = ref<7 | 30>(7)
const reasoningEffort = ref<'auto' | 'low' | 'medium' | 'high' | 'xhigh'>('auto')
const busy = ref<'daily' | 'relationship' | 'action' | null>(null)
const actionError = ref('')
const message = ref('')

const query = useQuery('insights', async () => {
  const [summaries, relationships, people] = await Promise.all([
    desktopApi.dailySummaries(),
    desktopApi.relationshipReports(),
    desktopApi.people(),
  ])
  return { summaries: summaries.items, relationships: relationships.items, people: people.items }
})

watchEffect(() => {
  if (!selectedPersonId.value && query.data.value?.people.length) {
    selectedPersonId.value = query.data.value.people[0].person_id
  }
})

const daily = computed(() => query.data.value?.summaries.find(
  (item) => item.summary_date === selectedDate.value && item.timezone === timezone.value,
) ?? query.data.value?.summaries[0] ?? null)

const relationship = computed(() => query.data.value?.relationships.find(
  (item) => item.person_id === selectedPersonId.value && item.window_days === windowDays.value,
) ?? null)

const selectedPerson = computed(() => query.data.value?.people.find(
  (item) => item.person_id === selectedPersonId.value,
) ?? null)

const sectionLabels: Record<string, string> = {
  what_happened: '今天发生了什么',
  decisions: '做出的决定',
  new_todos: '新增待办',
  completed: '已经完成',
  unresolved: '尚未解决',
  important_people_interactions: '重要人物互动',
  memorable_quotes: '值得记住的原话',
  tomorrow_attention: '明天要留意',
}

async function generateDaily(): Promise<void> {
  busy.value = 'daily'
  actionError.value = ''
  message.value = ''
  try {
    const result = await desktopApi.generateDailySummary(
      selectedDate.value, timezone.value, reasoningEffort.value,
    )
    message.value = `已从事件层生成 ${result.summary_date} 的第 ${result.revision} 版总结（${String(result.provenance.reasoning_effort ?? 'auto')}）。`
    await query.refresh(true)
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    busy.value = null
  }
}

async function generateRelationship(): Promise<void> {
  if (!selectedPersonId.value) return
  busy.value = 'relationship'
  actionError.value = ''
  message.value = ''
  try {
    const result = await desktopApi.generateRelationshipReport(
      selectedPersonId.value,
      windowDays.value,
      selectedDate.value,
      timezone.value,
      reasoningEffort.value,
    )
    message.value = `已生成 ${result.window_days} 天关系观察；事实与模型观察保持分栏。`
    await query.refresh(true)
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : String(error)
  } finally {
    busy.value = null
  }
}

function playEvidence(ids: string[], evidence: InsightEvidence[]): void {
  const item = evidence.find((value) => value.utterance_id && ids.includes(value.utterance_id))
  if (!item?.media_id) return
  playRange(item.media_id, item.start_ms ?? 0, item.end_ms ?? undefined)
}

async function reviseObservation(index: number): Promise<void> {
  const report = relationship.value
  const current = report?.observations[index]
  if (!report || !current) return
  const text = window.prompt('修正这条模型观察（证据、置信度和理由将继续保留）', current.text)
  if (text === null || !text.trim() || text.trim() === current.text) return
  const observations: RelationshipObservation[] = report.observations.map((item, itemIndex) => (
    itemIndex === index ? { ...item, text: text.trim() } : { ...item }
  ))
  await act(() => desktopApi.reviseRelationshipReport(report.report_id, observations))
}

async function retractReport(): Promise<void> {
  const report = relationship.value
  if (!report || !window.confirm('撤回这份关系观察？客观事实和不可变历史会保留。')) return
  await act(() => desktopApi.retractRelationshipReport(report.report_id))
}

async function undoReport(): Promise<void> {
  const report = relationship.value
  if (!report) return
  await act(() => desktopApi.undoRelationshipReport(report.report_id))
}

async function act(action: () => Promise<unknown>): Promise<void> {
  busy.value = 'action'
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

function eventTitle(item: Record<string, unknown>): string {
  return String(item.title || item.event_id || '未命名事件')
}

onBeforeUnmount(release)
</script>

<template>
  <section class="page insights-page">
    <header class="page-header">
      <div><p class="overline">GROUNDED INSIGHTS / V3.6</p><h1>总结与<em>关系</em></h1></div>
      <span class="header-note">程序核验事实 · Codex 组织语言 · 每条观察可追溯、可修正</span>
    </header>

    <section class="panel insight-runner">
      <label><span>日期 / 截止日</span><input v-model="selectedDate" type="date"></label>
      <label><span>业务时区</span><input v-model="timezone"></label>
      <label><span>推理强度</span><select v-model="reasoningEffort"><option value="auto">按需自动</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="xhigh">XHigh</option></select></label>
      <button class="primary-action" :disabled="busy !== null" @click="generateDaily">{{ busy === 'daily' ? '正在生成…' : '重建每日总结' }}</button>
    </section>
    <p v-if="message" class="generation-message">{{ message }}</p>
    <p v-if="actionError" class="reminder-error">{{ actionError }}</p>

    <PageState :loading="query.loading.value" :error="query.error.value" @retry="query.refresh(true)">
      <section v-if="daily" class="daily-insight-layout">
        <article class="panel verified-ledger">
          <header><div><p class="section-kicker">PROGRAM VERIFIED</p><h2>{{ daily.summary_date }} 客观底账</h2></div><span class="status-pill" :data-status="daily.derivation_status">{{ daily.derivation_status === 'stale' ? '源数据已改，建议重建' : `修订 ${daily.revision}` }}</span></header>
          <div class="insight-stat-grid">
            <span><small>事件</small><strong>{{ daily.objective.statistics.event_count ?? 0 }}</strong></span>
            <span><small>决定</small><strong>{{ daily.objective.statistics.decision_count ?? 0 }}</strong></span>
            <span><small>新增待办</small><strong>{{ daily.objective.statistics.new_todo_count ?? 0 }}</strong></span>
            <span><small>未解决</small><strong>{{ daily.objective.statistics.unresolved_count ?? 0 }}</strong></span>
          </div>
          <div class="verified-columns">
            <section><h3>决定</h3><p v-for="item in daily.objective.decisions" :key="String(item.event_id)">{{ eventTitle(item) }}</p><small v-if="!daily.objective.decisions.length">没有已核验决定</small></section>
            <section><h3>待办与约定</h3><p v-for="item in daily.objective.new_todos" :key="String(item.event_id)">{{ eventTitle(item) }}</p><small v-if="!daily.objective.new_todos.length">没有新增事项</small></section>
            <section><h3>遗留事项</h3><p v-for="item in daily.objective.unresolved" :key="String(item.event_id)">{{ eventTitle(item) }}</p><small v-if="!daily.objective.unresolved.length">没有遗留事项</small></section>
          </div>
        </article>

        <article class="panel narrative-ledger">
          <header><div><p class="section-kicker">CODEX LANGUAGE LAYER</p><h2>语言总结</h2></div><small>{{ String(daily.provenance.model ?? 'Codex') }} · {{ String(daily.provenance.reasoning_effort ?? 'auto') }}</small></header>
          <section v-for="(items, section) in daily.narrative" :key="section" class="narrative-section">
            <h3>{{ sectionLabels[section] ?? section }}</h3>
            <article v-for="item in items" :key="item.text">
              <p>{{ item.text }}</p>
              <button v-if="item.evidence_utterance_ids.length" class="text-button" @click="playEvidence(item.evidence_utterance_ids, daily.evidence)">▶ 播放依据</button>
            </article>
            <small v-if="!items.length">没有证据充分的内容</small>
          </section>
        </article>
      </section>
      <div v-else class="state-panel empty-state">这个日期还没有总结。点击“重建每日总结”从事件层生成。</div>

      <section class="relationship-section">
        <header class="section-heading"><div><p class="section-kicker">7 / 30 DAY WINDOW</p><h2>关系观察</h2></div></header>
        <div class="panel relationship-runner">
          <label><span>人物</span><select v-model="selectedPersonId"><option value="" disabled>选择人物</option><option v-for="person in query.data.value?.people" :key="person.person_id" :value="person.person_id">{{ person.display_name }}</option></select></label>
          <label><span>观察窗口</span><select v-model="windowDays"><option :value="7">近 7 天</option><option :value="30">近 30 天</option></select></label>
          <button class="primary-action" :disabled="!selectedPersonId || busy !== null" @click="generateRelationship">{{ busy === 'relationship' ? '正在生成…' : '生成关系观察' }}</button>
        </div>

        <div v-if="relationship" class="relationship-grid">
          <article class="panel relationship-facts">
            <header><div><p class="section-kicker">VERIFIED FACTS</p><h3>{{ selectedPerson?.display_name }} · {{ relationship.window_days }} 天</h3></div><span class="status-pill" :data-status="relationship.derivation_status">{{ relationship.derivation_status }}</span></header>
            <dl>
              <div><dt>本期互动会话</dt><dd>{{ relationship.verified_facts.interaction_frequency?.current_session_count ?? 0 }}</dd></div>
              <div><dt>较上期变化</dt><dd>{{ relationship.verified_facts.interaction_frequency?.session_delta ?? 0 }}</dd></div>
              <div><dt>未完成承诺</dt><dd>{{ relationship.verified_facts.unfinished_commitments?.length ?? 0 }}</dd></div>
              <div><dt>距上次联系</dt><dd>{{ relationship.verified_facts.days_since_contact ?? '—' }} 天</dd></div>
            </dl>
            <section><h4>本期主题</h4><span v-for="topic in relationship.verified_facts.topics?.current ?? []" :key="topic.label">{{ topic.label }} × {{ topic.count }}</span><small v-if="!(relationship.verified_facts.topics?.current.length)">没有已核验主题</small></section>
          </article>

          <article class="panel relationship-model">
            <header><div><p class="section-kicker">MODEL OBSERVATIONS</p><h3>模型观察（不是事实）</h3></div><div class="run-actions"><button v-if="relationship.status === 'active'" class="danger-button" :disabled="busy !== null" @click="retractReport">撤回整份观察</button><button v-else :disabled="busy !== null" @click="undoReport">恢复</button></div></header>
            <article v-for="(item, index) in relationship.observations" :key="`${relationship.revision}-${index}`" class="observation-card">
              <div><strong>{{ Math.round(item.confidence * 100) }}%</strong><span>模型置信度</span></div>
              <p>{{ item.text }}</p><small>理由：{{ item.rationale }}</small>
              <footer><button v-if="item.evidence_utterance_ids.length" class="text-button" @click="playEvidence(item.evidence_utterance_ids, relationship.evidence)">▶ 播放依据</button><button class="text-button" :disabled="relationship.status !== 'active' || busy !== null" @click="reviseObservation(index)">修正观察</button></footer>
            </article>
            <div v-if="!relationship.observations.length" class="state-panel empty-state">这个窗口没有证据充分的模型观察。</div>
          </article>
        </div>
        <div v-else-if="selectedPersonId" class="state-panel empty-state">尚未为这个人物生成 {{ windowDays }} 天关系观察。</div>
      </section>
    </PageState>
  </section>
</template>

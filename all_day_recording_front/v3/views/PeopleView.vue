<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watchEffect } from 'vue'

import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatDate } from '../core/format'
import { playRange, release } from '../core/media'
import { useQuery } from '../core/query'
import type { SpeakerCluster } from '../core/types'

const query = useQuery('people', async () => {
  const [clusters, people, sessions] = await Promise.all([
    desktopApi.speakerClusters(), desktopApi.people(), desktopApi.sessions(),
  ])
  return { clusters: clusters.items, people: people.items, sessions: sessions.items }
})
const selectedSession = ref('')
const selectedId = ref('')
const detail = ref<SpeakerCluster | null>(null)
const selectedPerson = ref('')
const newPersonName = ref('')
const mergeSource = ref('')
const splitTracks = ref<string[]>([])
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
})

const activeClusters = computed(() => query.data.value?.clusters.filter((item) => item.status === 'active') ?? [])
const mergeChoices = computed(() => activeClusters.value.filter((item) => item.cluster_id !== selectedId.value))

async function selectCluster(clusterId: string): Promise<void> {
  selectedId.value = clusterId
  splitTracks.value = []
  try { detail.value = await desktopApi.speakerCluster(clusterId) }
  catch (error) { errorMessage.value = error instanceof Error ? error.message : String(error) }
}

async function act(action: () => Promise<unknown>, message: string): Promise<void> {
  busy.value = true
  errorMessage.value = ''
  resultMessage.value = ''
  try {
    await action()
    if (message) resultMessage.value = message
    await query.refresh(true)
    if (selectedId.value) detail.value = await desktopApi.speakerCluster(selectedId.value).catch(() => null)
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

function labelExisting(): void {
  if (!detail.value || !selectedPerson.value) return
  void act(() => desktopApi.labelSpeaker(detail.value!.cluster_id, selectedPerson.value), '已确认人物；稳定声纹原型和相关事件引用已更新。')
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

onBeforeUnmount(release)
</script>

<template>
  <section class="page people-page">
    <header class="page-header">
      <div><p class="overline">OPEN-SET SPEAKER IDENTITY</p><h1>人物与<em>声纹</em></h1></div>
      <span class="header-note">“暂时无法判断”是合法结论；只有你的确认会进入稳定声纹库</span>
    </header>

    <section class="panel speaker-runner">
      <div><p class="section-kicker">LOCAL CAM++</p><h2>分析一段录音</h2><p>音频仅在本机读取。重复人物会跨录音聚合，人物相似只显示为“可能”。</p></div>
      <label><span>录音会话</span><select v-model="selectedSession"><option value="" disabled>选择会话</option><option v-for="session in query.data.value?.sessions" :key="session.session_id" :value="session.session_id">{{ formatDate(session.captured_start) }} · {{ session.session_id.slice(0, 8) }}</option></select></label>
      <button class="primary-action" :disabled="!selectedSession || busy" @click="analyze">{{ busy ? '本机分析中…' : '开始聚类' }}</button>
    </section>

    <p v-if="errorMessage" class="form-error speaker-message">{{ errorMessage }}</p>
    <p v-if="resultMessage" class="generation-message">{{ resultMessage }}</p>

    <PageState :loading="query.loading.value" :error="query.error.value" :empty="!query.data.value?.clusters.length" empty-text="还没有说话人簇。选择一段已处理录音开始本机分析。" @retry="query.refresh(true)">
      <div class="people-layout">
        <aside class="panel cluster-list">
          <button v-for="cluster in query.data.value?.clusters" :key="cluster.cluster_id" :class="{ active: selectedId === cluster.cluster_id }" @click="selectCluster(cluster.cluster_id)">
            <span class="speaker-mark">{{ cluster.person_name?.slice(0, 1) ?? '?' }}</span>
            <span><strong>{{ cluster.person_name ?? cluster.display_label }}</strong><small>{{ cluster.session_count }} 段录音 · {{ cluster.track_count }} 条说话轨</small><small v-if="cluster.suggested_person_id">可能匹配 · {{ Math.round((cluster.suggestion_confidence ?? 0) * 100) }}%</small></span>
            <i>{{ cluster.status }}</i>
          </button>
        </aside>

        <section v-if="detail" class="cluster-workspace">
          <article class="panel cluster-hero">
            <header><div><p class="section-kicker">CLUSTER {{ detail.cluster_id.slice(0, 8) }}</p><h2>{{ detail.person_name ?? detail.display_label }}</h2></div><span class="status-pill">{{ detail.person_name ? '已确认' : detail.suggested_person_id ? '可能匹配' : '暂无法判断' }}</span></header>
            <div class="cluster-metrics"><span><small>跨录音</small><strong>{{ detail.session_count }}</strong></span><span><small>说话轨</small><strong>{{ detail.track_count }}</strong></span><span><small>候选原型</small><strong>{{ detail.prototypes?.filter((item) => item.status === 'candidate').length ?? 0 }}</strong></span><span><small>版本</small><strong>r{{ detail.revision }}</strong></span></div>
            <div class="representative-clips"><small>代表片段</small><button v-for="clip in detail.prototypes?.flatMap((item) => item.representative_clips).slice(0, 5)" :key="`${clip.media_id}-${clip.start_ms}`" class="quiet-button" @click="playRange(clip.media_id, clip.start_ms, clip.end_ms)">▶ {{ Math.round((clip.end_ms - clip.start_ms) / 1000) }} 秒</button><span v-if="!detail.prototypes?.length">暂无可播放片段</span></div>
          </article>

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

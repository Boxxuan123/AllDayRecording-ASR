<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatDate, statusLabel } from '../core/format'
import { watchProcessingEvents } from '../core/processingEvents'
import { invalidateQuery, useQuery } from '../core/query'
import type { ProcessingSnapshot } from '../core/types'

const status = ref(new URLSearchParams(location.search).get('status') ?? '')
const requestedJob = new URLSearchParams(location.search).get('job')
const jobs = useQuery('processing-jobs', () => desktopApi.processingJobs(status.value))
const selected = ref(requestedJob)
const snapshot = ref<ProcessingSnapshot | null>(null)
const detailLoading = ref(false)
const detailError = ref<Error | null>(null)
const acting = ref(false)
let closeEvents: () => void = () => undefined

watch(() => jobs.data.value, (value) => {
  if (!selected.value && value?.items.length) selected.value = value.items[0].job_id
}, { immediate: true })
watch(selected, () => void loadSnapshot(), { immediate: true })

async function loadSnapshot(): Promise<void> {
  if (!selected.value) { snapshot.value = null; return }
  detailLoading.value = true
  detailError.value = null
  try { snapshot.value = await desktopApi.processingJob(selected.value) }
  catch (error) { detailError.value = error instanceof Error ? error : new Error(String(error)) }
  finally { detailLoading.value = false }
}

async function refresh(): Promise<void> {
  invalidateQuery('processing-jobs')
  await Promise.all([jobs.refresh(true), loadSnapshot()])
}

async function retry(): Promise<void> {
  if (!selected.value || !window.confirm('重试会为失败 stage 创建新 attempt；旧日志、配置和 checkpoint 将保留。继续吗？')) return
  acting.value = true
  try { snapshot.value = await desktopApi.retryJob(selected.value); await jobs.refresh(true) }
  finally { acting.value = false }
}

async function cancel(): Promise<void> {
  if (!selected.value || !window.confirm('取消会在下一个安全 checkpoint 生效，不会删除已完成制品。继续吗？')) return
  acting.value = true
  try { snapshot.value = await desktopApi.cancelJob(selected.value, 'cancelled from V3 Desktop'); await jobs.refresh(true) }
  finally { acting.value = false }
}

function setStatus(value: string): void {
  status.value = value
  history.replaceState({}, '', value ? `/processing?status=${encodeURIComponent(value)}` : '/processing')
  void jobs.refresh(true)
}

onMounted(() => { closeEvents = watchProcessingEvents(() => void refresh()) })
onBeforeUnmount(() => closeEvents())
</script>

<template>
  <section class="page processing-page">
    <header class="page-header"><div><p class="overline">DURABLE ORCHESTRATION</p><h1>处理中心</h1></div><span class="live-label"><i></i>SSE / REST RECOVERY</span></header>
    <div class="filter-tabs process-filters"><button v-for="item in [['','全部'],['running','运行中'],['failed_retryable','需重试'],['succeeded','已完成'],['cancelled','已取消']]" :key="item[0]" :class="{ active: status === item[0] }" @click="setStatus(item[0])">{{ item[1] }}</button></div>
    <PageState :loading="jobs.loading.value" :error="jobs.error.value" :empty="!jobs.data.value?.items.length" empty-text="当前没有处理任务。" @retry="refresh">
      <div class="processing-layout">
        <aside class="panel job-list">
          <button v-for="job in jobs.data.value?.items" :key="job.job_id" :class="{ active: selected === job.job_id }" @click="selected = job.job_id">
            <span><strong>{{ job.current_stage ?? '等待 worker' }}</strong><small>{{ job.session_id.slice(-10) }} · {{ formatDate(job.updated_at) }}</small></span>
            <span class="status-pill" :data-status="job.status">{{ statusLabel(job.status) }}</span>
            <i><b :style="{ width: `${job.progress * 100}%` }"></b></i>
          </button>
        </aside>
        <section class="panel run-inspector">
          <PageState :loading="detailLoading" :error="detailError" :empty="!snapshot" @retry="loadSnapshot">
            <template v-if="snapshot">
              <header><div><p class="section-kicker">RUN / {{ snapshot.run.run_id.slice(-10) }}</p><h2>{{ snapshot.run.pipeline_version }}</h2></div><div class="run-actions"><button v-if="snapshot.job.status === 'failed_retryable' || snapshot.job.status === 'stale'" :disabled="acting" @click="retry">重试</button><button v-if="snapshot.job.status === 'running' || snapshot.job.status === 'queued'" class="danger-button" :disabled="acting" @click="cancel">取消</button></div></header>
              <div class="run-summary"><span><small>会话</small><strong>{{ snapshot.run.session_id.slice(-12) }}</strong></span><span><small>run revision</small><strong>{{ snapshot.run.revision }}</strong></span><span><small>总进度</small><strong>{{ Math.round(snapshot.run.progress * 100) }}%</strong></span><span><small>状态</small><strong>{{ statusLabel(snapshot.job.status) }}</strong></span></div>
              <div class="stage-list">
                <article v-for="stage in snapshot.stages" :key="stage.stage_run_id">
                  <span class="stage-index">{{ String(stage.ordinal + 1).padStart(2, '0') }}</span><span class="stage-copy"><strong>{{ stage.stage }}</strong><small>{{ stage.optional ? '可选阶段' : '必需阶段' }}<template v-if="stage.error"> · {{ stage.error }}</template></small></span><span class="status-pill" :data-status="stage.status">{{ statusLabel(stage.status) }}</span>
                  <details v-if="snapshot.attempts.some((item) => item.stage_run_id === stage.stage_run_id)"><summary>attempt</summary><div v-for="attempt in snapshot.attempts.filter((item) => item.stage_run_id === stage.stage_run_id)" :key="attempt.attempt_id" class="attempt"><b>#{{ attempt.attempt_number }} · {{ statusLabel(attempt.status) }}</b><span>{{ attempt.worker_id }}</span><code>{{ attempt.log_summary ?? attempt.error ?? JSON.stringify(attempt.checkpoint ?? {}) }}</code></div></details>
                </article>
              </div>
            </template>
          </PageState>
        </section>
      </div>
    </PageState>
  </section>
</template>

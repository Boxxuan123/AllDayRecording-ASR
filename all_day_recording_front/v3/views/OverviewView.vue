<script setup lang="ts">
import { desktopApi } from '../core/api'
import { useQuery } from '../core/query'
import { navigate } from '../core/router'

const query = useQuery('overview', desktopApi.overview)
const todayLabel = (() => {
  const parts = new Intl.DateTimeFormat('en-GB', {
    weekday: 'long', day: '2-digit', month: 'short', year: 'numeric',
  }).formatToParts(new Date())
  const value = (type: Intl.DateTimeFormatPartTypes): string => (
    parts.find((part) => part.type === type)?.value ?? ''
  )
  return `${value('weekday')} · ${value('day')} ${value('month')} ${value('year')}`.toUpperCase()
})()
const statusLabel: Record<string, string> = {
  processing: '处理中',
  available: '可用',
  backup_required: '等待备份',
}

function dateTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(value))
}

function duration(value: number): string {
  const minutes = Math.floor(value / 60_000)
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}

function bytes(value: number): string {
  return `${(value / 1024 ** 3).toFixed(1)} GB`
}
</script>

<template>
  <section class="page overview-page">
    <header class="page-header">
      <div><p class="overline">{{ todayLabel }}</p><h1>你的记录，<em>正在成为证据。</em></h1></div>
      <button class="quiet-button" :disabled="query.loading.value" @click="query.refresh(true)">
        {{ query.loading.value ? '刷新中' : '刷新状态' }}
      </button>
    </header>

    <div v-if="query.error.value" class="state-panel error-state">
      <strong>无法读取本地 Core</strong><p>{{ query.error.value.message }}</p>
      <button @click="query.refresh(true)">重试</button>
    </div>
    <div v-else-if="query.loading.value" class="state-panel"><span class="spinner"></span>正在读取本地底账…</div>
    <template v-else-if="query.data.value">
      <div class="metric-strip">
        <article><small>已接纳会话</small><strong>{{ query.data.value.counts.sessions }}</strong><span>全部保留 revision</span></article>
        <article><small>正在处理</small><strong>{{ query.data.value.counts.active_jobs }}</strong><span>{{ query.data.value.counts.retryable_jobs }} 项需要处理</span></article>
        <article><small>独立备份</small><strong>{{ query.data.value.counts.backed_up_sessions }}/{{ query.data.value.counts.sessions }}</strong><span>恢复演练已核验</span></article>
        <article><small>原音体量</small><strong>{{ bytes(query.data.value.counts.audio_bytes) }}</strong><span>{{ query.data.value.counts.active_devices }} 台活跃设备</span></article>
      </div>

      <div class="overview-grid">
        <section class="panel recent-panel">
          <header><div><p class="section-kicker">RECENT RECORDINGS</p><h2>最近录音</h2></div><button @click="navigate('/recordings')">查看全部</button></header>
          <button
            v-for="session in query.data.value.recent_sessions"
            :key="session.session_id"
            class="session-row"
            @click="navigate(`/recordings/${encodeURIComponent(session.session_id)}?tab=overview`)"
          >
            <span class="session-date"><b>{{ dateTime(session.captured_start).split(' ')[0] }}</b><small>{{ dateTime(session.captured_start).split(' ')[1] }}</small></span>
            <span class="session-copy"><strong>{{ duration(session.duration_ms) }}</strong><small>{{ session.segment_count }} 个连续分片 · revision {{ session.revision }}</small></span>
            <span class="status-pill" :data-status="session.status_code">{{ statusLabel[session.status_code] ?? session.status_code }}</span>
            <span class="row-arrow">↗</span>
          </button>
        </section>

        <aside class="panel process-panel">
          <header><div><p class="section-kicker">DURABLE PROCESSING</p><h2>处理脉搏</h2></div><span class="live-label"><i></i>LIVE</span></header>
          <div v-if="!query.data.value.active_jobs.length" class="empty-inline">当前队列安静，所有任务都有持久终态。</div>
          <article v-for="job in query.data.value.active_jobs" :key="job.job_id" class="job-card">
            <div class="job-heading"><span>{{ job.current_stage }}</span><b>{{ Math.round(job.progress * 100) }}%</b></div>
            <div class="progress-track"><i :style="{ width: `${job.progress * 100}%` }"></i></div>
            <strong>{{ job.completed_stage_count }}/{{ job.stage_count }} 阶段已完成</strong>
            <small>{{ job.pipeline_version }} · run {{ job.run_id.slice(-6) }}</small>
          </article>
          <button class="process-link" @click="navigate('/processing')">打开处理中心 <span>→</span></button>
        </aside>
      </div>
    </template>
  </section>
</template>

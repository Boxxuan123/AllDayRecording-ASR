<script setup lang="ts">
import { computed } from 'vue'

import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatDate } from '../core/format'
import { useQuery } from '../core/query'
import { navigate } from '../core/router'
import type { ReviewItem, ReviewKind } from '../core/types'

const query = useQuery('reviews', desktopApi.reviews)
const items = computed(() => query.data.value?.items ?? [])

const kindLabels: Record<ReviewKind, string> = {
  processing_gate: '处理放行',
  reminder: '提醒确认',
  person_memory: '人物记忆',
  voice_identity: '声音身份',
}

const kindNotes: Record<ReviewKind, string> = {
  processing_gate: '流程明确停在人工关口，判断后才能继续。',
  reminder: '系统从原话生成了提醒，但不会在你确认前执行。',
  person_memory: '这条跨日人物记忆来自事件投影，尚未获得人工确认。',
  voice_identity: '音质达到审核门槛，且模型指向一个已知人物。',
}

const counts = computed(() => Object.fromEntries(
  (Object.keys(kindLabels) as ReviewKind[]).map((kind) => [
    kind,
    items.value.filter((item) => item.kind === kind).length,
  ]),
) as Record<ReviewKind, number>)

function cardTitle(item: ReviewItem): string {
  if (item.kind === 'processing_gate') return `${item.title} 阶段等待放行`
  if (item.kind === 'person_memory') return `${item.title} · 待核对的记忆`
  if (item.kind === 'voice_identity') return `这段声音是否属于 ${item.title}`
  return item.title
}

function cardSummary(item: ReviewItem): string {
  if (item.kind === 'processing_gate') {
    return item.context.error ? String(item.context.error) : '处理流程已暂停，等待人工判断。'
  }
  if (item.kind === 'reminder') {
    const schedule = item.context.scheduled_at ? formatDate(String(item.context.scheduled_at)) : '时间未确定'
    const location = item.context.location ? ` · ${item.context.location}` : ''
    return `${schedule}${location}`
  }
  if (item.kind === 'voice_identity') {
    const quality = Math.round(Number(item.context.quality_score ?? 0) * 100)
    const score = item.context.best_score == null
      ? '无相似度分数'
      : `匹配分 ${Number(item.context.best_score).toFixed(2)}`
    return `样本质量 ${quality}% · ${score}`
  }
  return item.summary
}

function targetFor(item: ReviewItem): string {
  if (item.kind === 'processing_gate' && item.session_id) {
    return `/recordings/${encodeURIComponent(item.session_id)}?tab=runs`
  }
  if (item.kind === 'reminder') {
    return `/reminders?candidate=${encodeURIComponent(item.source_id)}`
  }
  if (item.kind === 'person_memory' && item.person_id) {
    return `/people?mode=memory&person=${encodeURIComponent(item.person_id)}&memory=${encodeURIComponent(item.source_id)}`
  }
  if (item.kind === 'voice_identity' && item.person_id) {
    return `/people?mode=voice&person=${encodeURIComponent(item.person_id)}&prototype=${encodeURIComponent(item.source_id)}`
  }
  return '/reviews'
}

function targetLabel(item: ReviewItem): string {
  if (item.kind === 'processing_gate') return '查看处理阶段'
  if (item.kind === 'reminder') return '进入提醒确认'
  if (item.kind === 'person_memory') return '查看人物记忆'
  return '试听并核对身份'
}
</script>

<template>
  <section class="page">
    <header class="page-header"><div><p class="overline">REVIEW INBOX</p><h1>审核收件箱</h1></div><span class="header-note">只聚合真正需要你判断的证据，不收处理失败</span></header>
    <PageState :loading="query.loading.value" :error="query.error.value" :empty="!items.length" empty-text="当前四类来源都没有待判断项目：流程放行、提醒确认、人物记忆和已知人物声音。处理失败仍在处理中心。" @retry="query.refresh(true)">
      <div class="review-summary panel">
        <article v-for="(label, kind) in kindLabels" :key="kind" :data-kind="kind">
          <small>{{ label }}</small><strong>{{ counts[kind] }}</strong>
        </article>
      </div>
      <div class="review-grid">
        <article v-for="item in items" :key="item.review_id" class="panel review-card" :data-kind="item.kind" :data-priority="item.priority">
          <header>
            <span class="review-kind">{{ kindLabels[item.kind] }}</span>
            <span v-if="item.priority === 'high'" class="review-priority">优先处理</span>
          </header>
          <h2>{{ cardTitle(item) }}</h2>
          <p class="review-card-summary">{{ cardSummary(item) }}</p>
          <p class="review-card-reason">{{ kindNotes[item.kind] }}</p>
          <dl>
            <div><dt>证据</dt><dd>{{ item.evidence_count ? `${item.evidence_count} 条` : '流程状态' }}</dd></div>
            <div><dt>进入队列</dt><dd>{{ formatDate(item.created_at) }}</dd></div>
            <div v-if="item.session_id"><dt>会话</dt><dd>{{ item.session_id.slice(-10) }}</dd></div>
          </dl>
          <footer>
            <small>原始状态仍由对应功能页维护</small>
            <button class="primary-action" @click="navigate(targetFor(item))">{{ targetLabel(item) }} <b>→</b></button>
          </footer>
        </article>
      </div>
    </PageState>
  </section>
</template>

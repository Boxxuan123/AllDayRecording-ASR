<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { copyText } from '../core/clipboard'
import {
  formatDate,
  formatDuration,
  formatTimezone,
  sessionTitle,
  shortSessionId,
  stageLabel,
  statusLabel,
} from '../core/format'
import { useQuery } from '../core/query'
import { navigate } from '../core/router'
import type { SessionPage, SessionSummary } from '../core/types'

const query = useQuery('recordings', () => desktopApi.sessions())
const status = ref(new URLSearchParams(location.search).get('status') ?? 'all')
const search = ref('')
const searchResult = ref<SessionPage | null>(null)
const searchPending = ref(false)
const searchError = ref<Error | null>(null)
const copyNotice = ref('')
let searchTimer: number | undefined
let noticeTimer: number | undefined
let searchVersion = 0

const normalizedSearch = computed(() => search.value.trim())
const sourceItems = computed(() => normalizedSearch.value
  ? searchResult.value?.items ?? []
  : query.data.value?.items ?? [])
const filtered = computed(() => sourceItems.value.filter((item) =>
  status.value === 'all'
  || item.status_code === status.value
  || item.processing_status === status.value,
))
const pageLoading = computed(() => query.loading.value || searchPending.value)
const pageError = computed(() => normalizedSearch.value ? searchError.value : query.error.value)

watch(normalizedSearch, (value) => {
  window.clearTimeout(searchTimer)
  searchVersion += 1
  searchError.value = null
  if (!value) {
    searchPending.value = false
    searchResult.value = null
    return
  }
  searchPending.value = true
  const version = searchVersion
  searchTimer = window.setTimeout(() => void loadSearch(value, version), 260)
})

onBeforeUnmount(() => {
  window.clearTimeout(searchTimer)
  window.clearTimeout(noticeTimer)
})

async function loadSearch(value: string, version: number): Promise<void> {
  try {
    const result = await desktopApi.sessions(value)
    if (version !== searchVersion) return
    searchResult.value = result
  } catch (error) {
    if (version !== searchVersion) return
    searchError.value = error instanceof Error ? error : new Error(String(error))
  } finally {
    if (version === searchVersion) searchPending.value = false
  }
}

function retry(): void {
  if (!normalizedSearch.value) {
    void query.refresh(true)
    return
  }
  searchVersion += 1
  searchPending.value = true
  void loadSearch(normalizedSearch.value, searchVersion)
}

function setStatus(value: string): void {
  status.value = value
  const target = value === 'all' ? '/recordings' : `/recordings?status=${encodeURIComponent(value)}`
  history.replaceState({}, '', target)
}

function openSession(item: SessionSummary): void {
  navigate(`/recordings/${encodeURIComponent(item.session_id)}?tab=overview`)
}

async function copySessionId(item: SessionSummary): Promise<void> {
  const copied = await copyText(item.session_id)
  copyNotice.value = copied ? `已复制 ${shortSessionId(item.session_id)}` : '复制失败，请手动选择 Session ID'
  window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => { copyNotice.value = '' }, 2400)
}
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div><p class="overline">RECORDING CATALOG</p><h1>录音库</h1></div>
      <span class="header-note">原音不可变 · 以本地版本记录为准</span>
    </header>

    <div class="toolbar panel-flat recording-toolbar">
      <label class="search-field">
        <span>查找会话</span>
        <input v-model="search" type="search" placeholder="搜索日期、转写、说话人或 Session ID" />
      </label>
      <div class="filter-tabs" aria-label="处理状态筛选">
        <button
          v-for="item in [['all','全部'],['processing','处理中'],['available','可用'],['backup_required','待备份'],['failed_retryable','需重试']]"
          :key="item[0]"
          :class="{ active: status === item[0] }"
          :aria-pressed="status === item[0]"
          @click="setStatus(item[0])"
        >{{ item[1] }}</button>
      </div>
    </div>

    <div class="catalog-summary" aria-live="polite">
      <span>{{ normalizedSearch ? `找到 ${filtered.length} 条录音` : `共 ${filtered.length} 条录音` }}</span>
      <span v-if="searchPending">正在检索本地转写…</span>
      <span v-else-if="copyNotice" class="success-note">{{ copyNotice }}</span>
    </div>

    <PageState
      :loading="pageLoading"
      :error="pageError"
      :empty="!filtered.length"
      :empty-text="normalizedSearch ? '没有找到匹配的录音，可换一个日期、说话人或关键词。' : '当前筛选下没有录音会话。'"
      @retry="retry"
    >
      <div class="catalog panel">
        <div class="catalog-columns" aria-hidden="true">
          <span>录音</span><span>会话标识</span><span>内容</span><span>原音状态</span><span>处理状态</span><span>操作</span>
        </div>
        <article
          v-for="item in filtered"
          :key="item.session_id"
          class="catalog-row"
          role="link"
          tabindex="0"
          :aria-label="`打开${formatDate(item.captured_start)}的${sessionTitle(item.captured_start)}`"
          @click="openSession(item)"
          @keydown.enter.prevent="openSession(item)"
        >
          <span class="catalog-primary">
            <strong>{{ sessionTitle(item.captured_start) }}</strong>
            <small>{{ formatDate(item.captured_start) }} · {{ formatDuration(item.duration_ms) }}</small>
          </span>
          <span class="catalog-identity">
            <strong>{{ shortSessionId(item.session_id) }}</strong>
            <small>Session ID · {{ formatTimezone(item.timezone) }}</small>
          </span>
          <span>
            <strong>{{ item.segment_count }} 个分片</strong>
            <small>{{ stageLabel(item.current_stage) }} · 版本 {{ item.revision }}</small>
          </span>
          <span class="integrity"><i :data-ok="!item.blocking_reason"></i>{{ item.blocking_reason ? '准入受阻' : '原音已接纳' }}</span>
          <span class="status-pill" :data-status="item.status_code">{{ statusLabel(item.processing_status ?? item.status_code) }}</span>
          <span class="catalog-actions">
            <button class="mini-action" type="button" @click.stop="copySessionId(item)">复制 ID</button>
            <b aria-hidden="true">→</b>
          </span>
        </article>
      </div>
    </PageState>
  </section>
</template>

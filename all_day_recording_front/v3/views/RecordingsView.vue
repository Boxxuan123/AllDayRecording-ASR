<script setup lang="ts">
import { computed, ref } from 'vue'

import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatDate, formatDuration, statusLabel } from '../core/format'
import { useQuery } from '../core/query'
import { navigate } from '../core/router'

const query = useQuery('recordings', desktopApi.sessions)
const status = ref(new URLSearchParams(location.search).get('status') ?? 'all')
const search = ref('')
const filtered = computed(() => (query.data.value?.items ?? []).filter((item) =>
  (status.value === 'all' || item.status_code === status.value || item.processing_status === status.value)
  && (!search.value || item.session_id.toLowerCase().includes(search.value.toLowerCase())),
))

function setStatus(value: string): void {
  status.value = value
  const target = value === 'all' ? '/recordings' : `/recordings?status=${encodeURIComponent(value)}`
  history.replaceState({}, '', target)
}
</script>

<template>
  <section class="page">
    <header class="page-header"><div><p class="overline">RECORDING CATALOG</p><h1>录音库</h1></div><span class="header-note">原音不可变 · Core revision 为准</span></header>
    <div class="toolbar panel-flat">
      <label class="search-field"><span>查找会话</span><input v-model="search" placeholder="输入稳定 session ID" /></label>
      <div class="filter-tabs" aria-label="处理状态筛选">
        <button v-for="item in [['all','全部'],['processing','处理中'],['available','可用'],['backup_required','待备份'],['failed_retryable','需重试']]" :key="item[0]" :class="{ active: status === item[0] }" @click="setStatus(item[0])">{{ item[1] }}</button>
      </div>
    </div>
    <PageState :loading="query.loading.value" :error="query.error.value" :empty="!filtered.length" empty-text="当前筛选下没有录音会话。" @retry="query.refresh(true)">
      <div class="catalog panel">
        <button v-for="item in filtered" :key="item.session_id" class="catalog-row" @click="navigate(`/recordings/${encodeURIComponent(item.session_id)}?tab=overview`)">
          <span class="catalog-time"><strong>{{ formatDate(item.captured_start) }}</strong><small>{{ formatDuration(item.duration_ms) }}</small></span>
          <span><strong>{{ item.segment_count }} 个分片</strong><small>{{ item.timezone }} · revision {{ item.revision }}</small></span>
          <span class="integrity"><i :data-ok="!item.blocking_reason"></i>{{ item.blocking_reason ? '准入受阻' : '原音已接纳' }}</span>
          <span class="status-pill" :data-status="item.status_code">{{ statusLabel(item.processing_status ?? item.status_code) }}</span>
          <b>→</b>
        </button>
      </div>
    </PageState>
  </section>
</template>

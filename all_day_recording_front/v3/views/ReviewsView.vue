<script setup lang="ts">
import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatDate, statusLabel } from '../core/format'
import { useQuery } from '../core/query'
import { navigate } from '../core/router'

const query = useQuery('reviews', desktopApi.reviews)
</script>

<template>
  <section class="page">
    <header class="page-header"><div><p class="overline">REVIEW INBOX</p><h1>审核收件箱</h1></div><span class="header-note">这里只呈现需要人判断的证据</span></header>
    <PageState :loading="query.loading.value" :error="query.error.value" :empty="!query.data.value?.items.length" empty-text="当前没有等待人工判断的项目。处理失败请前往处理中心。" @retry="query.refresh(true)">
      <div class="panel list-panel">
        <button v-for="item in query.data.value?.items" :key="item.stage_run_id" class="catalog-row" @click="navigate(`/recordings/${encodeURIComponent(item.session_id)}?tab=runs`)">
          <span><strong>{{ item.stage }}</strong><small>{{ formatDate(item.updated_at) }}</small></span>
          <span><strong>会话 {{ item.session_id.slice(-8) }}</strong><small>run {{ item.run_id.slice(-8) }}</small></span>
          <span class="status-pill">{{ statusLabel(item.status) }}</span><b>→</b>
        </button>
      </div>
    </PageState>
  </section>
</template>

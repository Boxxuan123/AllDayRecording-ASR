<script setup lang="ts">
import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { useQuery } from '../core/query'

const query = useQuery('settings', desktopApi.settings)
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div><p class="overline">LOCAL POLICY</p><h1>设置</h1></div>
      <span class="header-note">
        <template v-if="query.data.value">V{{ query.data.value.contract_version }} · {{ query.data.value.deployment }}</template>
        <template v-else>读取已生效的安全边界</template>
      </span>
    </header>
    <PageState :loading="query.loading.value" :error="query.error.value" @retry="query.refresh(true)">
      <div v-if="query.data.value" class="settings-grid">
        <section class="panel setting-card">
          <p class="section-kicker">PROCESSING</p><h2>持久处理</h2>
          <label><span>所有长任务先落库</span><input type="checkbox" :checked="query.data.value.processing.durable_jobs" disabled /></label>
          <label><span>处理前强制备份准入</span><input type="checkbox" :checked="query.data.value.processing.backup_admission_required" disabled /></label>
          <label><span>完成后自动删除原音</span><input type="checkbox" :checked="query.data.value.processing.automatic_source_deletion" disabled /></label>
        </section>
        <section class="panel setting-card">
          <p class="section-kicker">PRIVACY</p><h2>本地优先</h2>
          <label><span>Desktop API 仅监听 loopback</span><input type="checkbox" :checked="query.data.value.privacy.network_boundary === 'loopback'" disabled /></label>
          <label><span>默认上传原音到云端</span><input type="checkbox" :checked="query.data.value.privacy.audio_cloud_upload" disabled /></label>
          <label><span>允许云端处理文字</span><input type="checkbox" :checked="query.data.value.privacy.transcript_cloud_processing" disabled /></label>
        </section>
        <section class="panel setting-card">
          <p class="section-kicker">CODEX</p><h2>提醒与洞察</h2>
          <label><span>Codex 提醒生成</span><input type="checkbox" :checked="query.data.value.reminders.codex_enabled" disabled /></label>
          <label><span>Codex 洞察生成</span><input type="checkbox" :checked="query.data.value.insights.codex_enabled" disabled /></label>
          <label><span>洞察保留版本化修正</span><input type="checkbox" :checked="query.data.value.insights.versioned_corrections" disabled /></label>
          <p>工作区：{{ query.data.value.insights.codex_workspace }}；来源层：{{ query.data.value.insights.source_layer }}；关系窗口：{{ query.data.value.insights.relationship_windows_days.join(' / ') }} 天。</p>
        </section>
      </div>
    </PageState>
  </section>
</template>

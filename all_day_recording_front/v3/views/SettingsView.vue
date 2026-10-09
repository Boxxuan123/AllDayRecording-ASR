<script setup lang="ts">
import { computed, ref } from 'vue'
import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { useQuery } from '../core/query'

const query = useQuery('settings', desktopApi.settings)
const diagnostics = useQuery('diagnostics', desktopApi.diagnostics)
interface BuildDetails {
  release_version?: string
  git_commit?: string
  built_at?: string | null
  dirty?: boolean | null
  identity_source?: string
}
const build = computed(() => diagnostics.data.value?.build as BuildDetails | undefined)
const frontend = computed(() => diagnostics.data.value?.frontend_build as BuildDetails | undefined)
const buildState = computed(() => build.value?.dirty === true ? '包含本地修改' :
  build.value?.dirty === false ? '已发布构建' : '状态未知')
function formatTime(value?: string | null) {
  if (!value) return '源码运行，未打包'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '未知' : date.toLocaleString('zh-CN', { hour12: false })
}
const copyStatus = ref('')
async function copyDiagnostics() {
  if (!diagnostics.data.value) return
  try {
    await navigator.clipboard.writeText(JSON.stringify(diagnostics.data.value, null, 2))
    copyStatus.value = '已复制'
  } catch {
    copyStatus.value = '复制失败，请展开原始数据手动复制'
  }
}
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div><p class="overline">LOCAL POLICY</p><h1>设置</h1></div>
      <span class="header-note">本机偏好与数据管理</span>
    </header>
    <PageState :loading="query.loading.value" :error="query.error.value" :has-content="query.data.value !== null" @retry="query.refresh(true)">
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
    <details class="about-settings">
      <summary>
        <span class="about-label"><strong>关于 AllDay</strong><small>版本与设备诊断</small></span>
        <span class="about-version">{{ build?.release_version || '读取中' }}</span>
        <svg class="about-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
      </summary>
      <div class="about-content" v-if="diagnostics.data.value">
        <div class="about-heading"><h2>AllDay Recording</h2><span>{{ buildState }}</span></div>
        <dl class="build-facts">
          <div><dt>程序版本</dt><dd>{{ build?.release_version }}</dd></div>
          <div><dt>运行方式</dt><dd>{{ build?.identity_source === 'source-checkout' ? '本机源码' : '安装包' }}</dd></div>
          <div><dt>构建时间</dt><dd>{{ formatTime(build?.built_at) }}</dd></div>
          <div><dt>界面构建时间</dt><dd>{{ formatTime(frontend?.built_at) }}</dd></div>
          <div class="commit-fact"><dt>Git 提交</dt><dd>{{ build?.git_commit || '未知' }}</dd></div>
          <div><dt>通信协议</dt><dd>{{ diagnostics.data.value.contract_version }}</dd></div>
          <div><dt>数据库迁移</dt><dd>{{ diagnostics.data.value.database_schema_version }}</dd></div>
        </dl>
        <div class="diagnostic-actions">
          <button class="diagnostic-copy" @click="copyDiagnostics">复制诊断信息</button>
          <span role="status">{{ copyStatus }}</span>
        </div>
        <details class="raw-diagnostics">
          <summary>原始诊断数据<span>含设备实际上报的信息</span></summary>
          <pre>{{ JSON.stringify(diagnostics.data.value, null, 2) }}</pre>
        </details>
      </div>
      <div v-else class="about-unavailable">
        <p>{{ diagnostics.error.value || '正在读取诊断信息…' }}</p>
        <button class="diagnostic-copy" @click="diagnostics.refresh(true)">重新读取</button>
      </div>
    </details>
  </section>
</template>

<style scoped>
.about-settings { margin-top: 28px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.about-settings > summary { display: flex; align-items: center; gap: 18px; padding: 22px 4px; cursor: pointer; list-style: none; }
.about-settings > summary::-webkit-details-marker, .raw-diagnostics > summary::-webkit-details-marker { display: none; }
.about-label { display: grid; gap: 5px; }
.about-label strong { font-size: 14px; font-weight: 600; }
.about-label small { color: var(--muted); font-size: 11px; }
.about-version { margin-left: auto; color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.about-chevron { width: 16px; height: 16px; fill: none; stroke: var(--muted); stroke-width: 1.5; transition: transform .15s; }
.about-settings[open] .about-chevron { transform: rotate(90deg); }
.about-settings > summary:focus-visible, .raw-diagnostics > summary:focus-visible { outline: 2px solid var(--teal); outline-offset: 4px; }
.about-content, .about-unavailable { padding: 0 4px 24px; }
.about-heading { display: flex; align-items: baseline; gap: 16px; margin: 4px 0 20px; }
.about-heading h2 { margin: 0; font: 500 21px Georgia, "STSong", serif; }
.about-heading span { color: var(--muted); font-size: 11px; }
.build-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 32px; margin: 0; }
.build-facts > div { display: grid; grid-template-columns: 105px minmax(0, 1fr); gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--line); font-size: 12px; }
.build-facts dt { color: var(--muted); }
.build-facts dd { margin: 0; overflow-wrap: anywhere; }
.build-facts .commit-fact { grid-column: 1 / -1; }
.commit-fact dd { font-family: Consolas, monospace; font-size: 11px; }
.diagnostic-actions { display: flex; align-items: center; gap: 14px; margin: 20px 0; }
.diagnostic-copy { padding: 9px 14px; background: var(--paper); border: 1px solid var(--line); border-radius: 8px; font-size: 12px; }
.diagnostic-copy:hover { border-color: var(--teal); color: var(--teal); }
.diagnostic-actions > span, .about-unavailable p { color: var(--muted); font-size: 12px; }
.raw-diagnostics > summary { display: flex; gap: 14px; width: fit-content; padding: 5px 0; cursor: pointer; list-style: none; font-size: 11px; color: var(--muted); }
.raw-diagnostics > summary span { opacity: .75; }
.raw-diagnostics pre { max-height: 340px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 18px; border: 1px solid var(--line); border-radius: 8px; background: var(--paper); font-size: 11px; line-height: 1.7; }
@media (max-width: 760px) { .build-facts { grid-template-columns: 1fr; } .about-heading { align-items: start; flex-direction: column; gap: 6px; } .raw-diagnostics > summary { flex-direction: column; gap: 4px; } }
</style>

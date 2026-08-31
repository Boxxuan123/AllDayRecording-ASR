<script setup lang="ts">
import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { useQuery } from '../core/query'
const query = useQuery('settings', desktopApi.settings)
</script>
<template><section class="page"><header class="page-header"><div><p class="overline">LOCAL POLICY</p><h1>设置</h1></div><span class="header-note">V3.0 只展示已生效的安全边界</span></header><PageState :loading="query.loading.value" :error="query.error.value" @retry="query.refresh(true)"><div class="settings-grid"><section class="panel setting-card"><p class="section-kicker">PROCESSING</p><h2>持久处理</h2><label><span>所有长任务先落库</span><input type="checkbox" checked disabled /></label><label><span>处理前强制备份准入</span><input type="checkbox" checked disabled /></label><label><span>完成后自动删除原音</span><input type="checkbox" disabled /></label></section><section class="panel setting-card"><p class="section-kicker">PRIVACY</p><h2>本地优先</h2><label><span>Desktop API 仅监听 loopback</span><input type="checkbox" checked disabled /></label><label><span>默认上传云端</span><input type="checkbox" disabled /></label><p>模型、缓存和通知策略将在对应能力接入后开放编辑，不在前端伪造未生效设置。</p></section></div></PageState></section></template>

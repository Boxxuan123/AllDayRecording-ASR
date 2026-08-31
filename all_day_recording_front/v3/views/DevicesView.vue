<script setup lang="ts">
import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { formatDate, statusLabel } from '../core/format'
import { useQuery } from '../core/query'

const query = useQuery('devices', desktopApi.devices)
const kindLabel: Record<string, string> = { watch: 'Watch', phone: 'Phone', computer: 'Computer' }
</script>
<template><section class="page"><header class="page-header"><div><p class="overline">DEVICE & TRUST</p><h1>设备与传输</h1></div><span class="header-note">一次授权 · HUKS 静默签名</span></header><PageState :loading="query.loading.value" :error="query.error.value" :empty="!query.data.value?.items.length" @retry="query.refresh(true)"><div class="device-grid"><article v-for="item in query.data.value?.items" :key="item.device_id" class="panel device-card"><span class="device-kind">{{ kindLabel[item.kind] ?? item.kind }}</span><h2>{{ item.name }}</h2><p>{{ item.paired ? `已绑定 ${item.receiver_id}` : '无需独立 Device API 配对' }}</p><dl><div><dt>状态</dt><dd>{{ statusLabel(item.status) }}</dd></div><div><dt>最近活动</dt><dd>{{ formatDate(item.last_seen_at) }}</dd></div><div><dt>revision</dt><dd>{{ item.revision }}</dd></div></dl></article></div></PageState></section></template>

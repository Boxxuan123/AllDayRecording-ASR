<script setup lang="ts">
import PageState from '../components/PageState.vue'
import { desktopApi } from '../core/api'
import { useQuery } from '../core/query'
const query = useQuery('lab', desktopApi.lab)
</script>
<template><section class="page lab-page"><header class="page-header"><div><p class="overline">EXPLICIT ADVANCED BOUNDARY</p><h1>实验室</h1></div><span class="lab-badge">不在普通导航</span></header><PageState :loading="query.loading.value" :error="query.error.value" @retry="query.refresh(true)"><section class="panel lab-boundary"><strong>LAB</strong><div><h2>{{ query.data.value?.label }}</h2><p>{{ query.data.value?.message }}</p><p>benchmark、truth、模型对比继续从明确的 Legacy/Lab 工具进入，不向普通产品页面泄漏实验术语和危险操作。</p></div></section></PageState></section></template>

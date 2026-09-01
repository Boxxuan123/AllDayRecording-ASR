<script setup lang="ts">
import { computed } from 'vue'

import { connectivity } from '../core/connectivity'
import { navigate, route, type RouteName } from '../core/router'

const navigation: { name: RouteName; label: string; eyebrow: string; path: string }[] = [
  { name: 'overview', label: '总览', eyebrow: 'NOW', path: '/' },
  { name: 'recordings', label: '录音库', eyebrow: 'AUDIO', path: '/recordings' },
  { name: 'reviews', label: '审核', eyebrow: 'INBOX', path: '/reviews' },
  { name: 'reminders', label: '提醒', eyebrow: 'ACT', path: '/reminders' },
  { name: 'people', label: '人物与声纹', eyebrow: 'VOICE', path: '/people' },
  { name: 'processing', label: '处理中心', eyebrow: 'RUNS', path: '/processing' },
  { name: 'devices', label: '设备与传输', eyebrow: 'LINK', path: '/devices' },
  { name: 'data', label: '数据与备份', eyebrow: 'SAFE', path: '/data' },
  { name: 'settings', label: '设置', eyebrow: 'LOCAL', path: '/settings' },
]

const active = computed(() => route.value.name)
</script>

<template>
  <div class="shell">
    <aside class="rail">
      <button class="brand" aria-label="返回总览" @click="navigate('/')">
        <span class="brand-signal" aria-hidden="true"><i></i><i></i><i></i></span>
        <span><strong>AllDay</strong><small>LOCAL MEMORY / V3</small></span>
      </button>
      <nav aria-label="主导航">
        <button
          v-for="item in navigation"
          :key="item.name"
          class="nav-link"
          :class="{ active: active === item.name || (active === 'session' && item.name === 'recordings') }"
          @click="navigate(item.path)"
        >
          <small>{{ item.eyebrow }}</small><span>{{ item.label }}</span><b aria-hidden="true">→</b>
        </button>
      </nav>
      <div class="local-trust">
        <span class="live-dot" :data-offline="!connectivity.online"></span>
        <div><strong>本机权威源</strong><small>{{ connectivity.online ? 'Loopback · 未连接云端' : '网络离线 · 本机数据仍可用' }}</small></div>
      </div>
    </aside>
    <main class="main"><slot /></main>
  </div>
</template>

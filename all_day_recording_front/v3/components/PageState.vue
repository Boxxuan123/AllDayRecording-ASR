<script setup lang="ts">
import { connectivity } from '../core/connectivity'

defineProps<{ loading: boolean; error: Error | null; empty?: boolean; emptyText?: string }>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div v-if="error && !connectivity.online" class="state-panel error-state offline-state">
    <strong>设备处于离线状态</strong><p>已加载内容仍留在本机；恢复连接后可重试读取 Core。</p><button @click="$emit('retry')">重新连接</button>
  </div>
  <div v-else-if="error" class="state-panel error-state">
    <strong>无法读取本地 Core</strong><p>{{ error.message }}</p><button @click="$emit('retry')">重试</button>
  </div>
  <div v-else-if="loading" class="state-panel"><span class="spinner"></span>正在读取本地底账…</div>
  <div v-else-if="empty" class="state-panel empty-state">{{ emptyText ?? '这里还没有记录。' }}</div>
  <slot v-else />
</template>

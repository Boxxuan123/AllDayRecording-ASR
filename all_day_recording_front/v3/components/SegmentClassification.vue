<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Utterance } from '../core/types'
import { desktopApi } from '../core/api'
import { soundKinds, soundKindLabel } from '../core/soundKinds'
import { formatOffset } from '../core/format'
const props = defineProps<{ items: Utterance[] }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const selected = ref<string[]>(props.items.length === 1 ? [props.items[0].utterance_id] : [])
const kind = ref('')
const page = ref(0)
const busy = ref(false)
const error = ref('')
const savedRows = ref<Utterance[]>([])
const rows = computed(() => props.items.slice(page.value * 20, (page.value + 1) * 20))
async function save() {
  busy.value = true
  error.value = ''
  try {
    const result = await desktopApi.classifySegments(props.items.filter((item) => selected.value.includes(item.utterance_id))
      .map((item) => ({ utterance_id: item.utterance_id, revision: item.revision })), kind.value)
    savedRows.value = result.utterances.filter(row => props.items.some(old => old.utterance_id === row.utterance_id && old.revision < row.revision))
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : '保存失败'
  } finally { busy.value = false }
}
async function undo() {
  busy.value = true
  error.value = ''
  try {
    await desktopApi.undoAnnotations(savedRows.value.map(row => ({ utterance_id: row.utterance_id, revision: row.revision })))
    emit('saved')
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '撤销失败' }
  finally { busy.value = false }
}
function close() { savedRows.value.length ? emit('saved') : emit('close') }
</script>
<template>
  <div class="dialog-backdrop" @click.self="!busy && close()">
    <section class="dialog segment-classification" role="dialog" aria-modal="true" aria-label="标注声音类型">
      <header><h2>标注声音类型</h2><button :disabled="busy" @click="close">关闭</button></header>
      <p v-if="savedRows.length">已更新 {{ savedRows.length }} 个片段，相关派生结果待更新。<button :disabled="busy" @click="undo">撤销本次分类</button></p>
      <p>原音与原始转写保留。清楚的重叠或远程对话仍可提取内容；媒体播放不作为实际互动。人物归属独立保留。</p>
      <div><button @click="selected = items.map((item) => item.utterance_id)">全选整组</button><button @click="selected = []">清空</button></div>
      <div class="segment-choices">
        <label v-for="item in rows" :key="item.utterance_id">
          <input v-model="selected" type="checkbox" :value="item.utterance_id" />
          <span>{{ formatOffset(item.start_ms) }} · {{ soundKindLabel(item) }}<br />{{ item.text }}</span>
        </label>
      </div>
      <div><button :disabled="page === 0" @click="page--">上一页</button><span>{{ page + 1 }} / {{ Math.max(1, Math.ceil(items.length / 20)) }}</span><button :disabled="(page + 1) * 20 >= items.length" @click="page++">下一页</button></div>
      <label><span>声音类型</span><select v-model="kind"><option disabled value="">请选择声音类型</option><option v-for="option in soundKinds" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
      <p v-if="error" role="alert">{{ error }}。如片段已更新，请关闭后刷新页面再核对。</p>
      <button class="primary-action" :disabled="busy || !selected.length || !kind || !!savedRows.length" @click="save">{{ busy ? '保存中…' : `保存所选 ${selected.length} 个片段` }}</button>
    </section>
  </div>
</template>
<style scoped>
.segment-classification{display:flex;flex-direction:column;max-height:85vh;width:min(700px,95vw)}
header{display:flex;justify-content:space-between;align-items:center}.segment-choices{overflow:auto;min-height:80px;flex:1}
.segment-choices label{display:flex;align-items:flex-start;gap:10px;padding:10px;border-bottom:1px solid #ddd}.segment-choices input{width:auto}
</style>

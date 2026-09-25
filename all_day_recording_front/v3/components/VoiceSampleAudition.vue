<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mediaState, playCompleteSample, stop } from '../core/media'
import { desktopApi } from '../core/api'
import type { VoicePrototypeCandidate } from '../core/types'

interface Window { media_id: string; start_ms: number; end_ms: number; playback_start_ms: number }
interface Plan { audition_key: string; windows: Window[]; total_ms: number; window_count: number; audio_available?: boolean; audio_unavailable_reason?: string; complete_sample?: boolean; data_base64url?: string }
const props = defineProps<{ candidate: VoicePrototypeCandidate }>()
const plan = ref<Plan | null>(null)
const complete = ref(false)
const busy = ref(false)
const error = ref('')
let generation = 0
let ownsPlayer = false
const current = computed(() => busy.value ? plan.value?.windows.find(w => mediaState.currentTimeMs >= w.playback_start_ms && mediaState.currentTimeMs < w.playback_start_ms + w.end_ms - w.start_ms) : null)
async function request(describe: boolean): Promise<Plan> {
  return desktopApi.voiceAudition<Plan>({ prototype_id: props.candidate.prototype_id, person_id: props.candidate.person_id,
    describe, ...(describe ? {} : { audition_key: plan.value?.audition_key }) })
}
function clear() { generation++; complete.value = false; busy.value = false; if (ownsPlayer) { ownsPlayer = false; stop() } }
async function load() {
  clear(); plan.value = null; error.value = ''
  const attempt = generation
  try { const result = await request(true); if (attempt === generation) plan.value = result }
  catch (e) { if (attempt === generation) error.value = String(e) }
}
async function play() {
  clear(); stop(); error.value = ''; busy.value = true
  const audioEpoch = mediaState.auditionGeneration
  const attempt = generation
  try {
    const result = await request(false)
    if (attempt !== generation) return
    if (audioEpoch !== mediaState.auditionGeneration) { busy.value = false; return }
    if (!result.complete_sample || result.audition_key !== plan.value?.audition_key || !result.data_base64url) throw new Error('服务器未提供完整样本')
    const binary = atob(result.data_base64url.replace(/-/g, '+').replace(/_/g, '/'))
    const bytes = Uint8Array.from(binary, c => c.charCodeAt(0))
    // stop() increments the shared generation synchronously before this owner starts.
    const playback = playCompleteSample(new Blob([bytes], { type: 'audio/wav' }), props.candidate.prototype_id)
    ownsPlayer = true
    await playback
    if (attempt === generation) { complete.value = true; busy.value = false }
  } catch (e) { if (attempt === generation) { complete.value = false; busy.value = false; error.value = String(e) } }
}
watch(() => mediaState.auditionGeneration, () => { complete.value = false; ownsPlayer = false }, { flush: 'sync' })
watch(() => props.candidate, load, { immediate: true, deep: true })
function hidden() { if (document.hidden) clear() }
document.addEventListener('visibilitychange', hidden)
onBeforeUnmount(() => { clear(); document.removeEventListener('visibilitychange', hidden) })
</script>

<template>
  <div class="sample-audition">
    <small>{{ candidate.representative_clips.length }} 个窗口 · {{ candidate.representative_clips.reduce((n, w) => n + w.end_ms - w.start_ms, 0) / 1000 }} 秒（不含间隙）</small>
    <small v-for="(w, i) in candidate.representative_clips" :key="`${w.media_id}:${w.start_ms}`">{{ i + 1 }}. {{ w.media_id }} · {{ w.start_ms / 1000 }}–{{ w.end_ms / 1000 }} 秒</small>
    <small v-if="current">正在播放：{{ current.media_id }} · {{ current.start_ms / 1000 }}–{{ current.end_ms / 1000 }} 秒</small>
    <button class="quiet-button" :disabled="!plan?.audio_available || busy" @click="play">▶ 完整串播</button>
    <button v-if="busy" class="quiet-button" @click="clear">停止试听</button>
    <small>{{ error || plan?.audio_unavailable_reason || (complete ? '所有窗口已播放完成，可以判断' : '完整播放结束后才能确认；开始播放不代表完成') }}</small>
    <slot :complete="complete" />
  </div>
</template>

<style scoped>
.sample-audition small { display: block; overflow-wrap: anywhere; }
</style>

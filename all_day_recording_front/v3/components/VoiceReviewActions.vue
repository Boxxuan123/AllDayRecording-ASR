<script lang="ts">
const expandedVoiceReviewIds = new Set<string>()
</script>

<script setup lang="ts">
import { computed, ref } from 'vue'

import VoiceSampleAudition from './VoiceSampleAudition.vue'
import type { VoicePrototypeCandidate, VoicePrototypeReviewStatus } from '../core/types'

const props = defineProps<{
  candidates: VoicePrototypeCandidate[]
  busyId: string
  reviewId: string
}>()

const emit = defineEmits<{
  review: [candidate: VoicePrototypeCandidate, decision: Exclude<VoicePrototypeReviewStatus, 'pending' | 'retracted'>]
}>()

const expanded = ref(expandedVoiceReviewIds.has(props.reviewId))
const sortedCandidates = computed(() => [...props.candidates].sort((left, right) => (
  comparableScore(left) - comparableScore(right)
  || left.quality_score - right.quality_score
  || left.prototype_id.localeCompare(right.prototype_id)
)))
const showSamples = computed(() => props.candidates.length === 1 || expanded.value)

function comparableScore(candidate: VoicePrototypeCandidate): number {
  return candidate.best_score ?? Number.NEGATIVE_INFINITY
}

function percentage(value: number | null): string {
  return value === null ? '未知' : `${Math.round(value * 100)}%`
}

function setExpanded(value: boolean): void {
  expanded.value = value
  if (value) expandedVoiceReviewIds.add(props.reviewId)
  else expandedVoiceReviewIds.delete(props.reviewId)
}

function busy(candidate: VoicePrototypeCandidate): boolean {
  return props.busyId === `${props.reviewId}:${candidate.prototype_id}`
}
</script>

<template>
  <div class="voice-sample-review">
    <div v-if="candidates.length > 1 && !expanded" class="voice-group-safety">
      <p>不会整组写入声纹库。展开后逐个试听，只有你单独确认的样本才会入库。</p>
      <div>
        <button
          type="button"
          class="review-action play"
          :disabled="!sortedCandidates[0]?.representative_clips.length"
          @click="setExpanded(true)"
        >展开试听最弱样本</button>
        <button type="button" class="review-action primary" @click="setExpanded(true)">核对 {{ candidates.length }} 个样本</button>
      </div>
    </div>

    <div v-if="showSamples" class="voice-sample-list">
      <article v-for="(candidate, index) in sortedCandidates" :key="candidate.prototype_id" class="voice-sample-row">
        <div>
          <strong>样本 {{ index + 1 }}<template v-if="candidates.length > 1 && index === 0"> · 最弱匹配</template></strong>
          <small>录音 {{ candidate.session_id.slice(-10) }} · 匹配 {{ percentage(candidate.best_score) }} · 音质 {{ percentage(candidate.quality_score) }}</small>
        </div>
        <VoiceSampleAudition :candidate="candidate" v-slot="{ complete }"><div class="voice-sample-actions">
          <button type="button" class="review-action" :disabled="busy(candidate)" @click="emit('review', candidate, 'uncertain')">暂不判断</button>
          <button type="button" class="review-action reject" :disabled="busy(candidate) || !complete" @click="emit('review', candidate, 'rejected')">不是此人</button>
          <button type="button" class="review-action primary" :disabled="busy(candidate) || !complete" @click="emit('review', candidate, 'confirmed')">{{ busy(candidate) ? '处理中' : '确认此样本' }}</button>
        </div></VoiceSampleAudition>
      </article>
      <button v-if="candidates.length > 1" type="button" class="text-button voice-sample-collapse" @click="setExpanded(false)">收起样本</button>
    </div>

    <p v-if="!candidates.length" class="voice-sample-missing">候选详情暂不可用，请进入证据页逐项核对。</p>
  </div>
</template>

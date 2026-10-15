import { computed } from 'vue'
import { useQuery } from './query'
import { desktopApi } from './api'

export function useReviewInbox(api = desktopApi) {
  const main = useQuery('reviews', signal => api.reviews(signal))
  const voiceEnabled = () => Boolean(main.data.value?.items.some(item => item.kind === 'voice_identity'
    && item.context.voice_mode !== 'speaker_discovery' && !Array.isArray(item.context.voice_candidates)))
  const reminderEnabled = () => Boolean(main.data.value?.items.some(item => item.kind === 'reminder'))
  const voice = useQuery('review-voice-candidates', signal => api.voicePrototypeCandidates('', 'pending', signal),
    voiceEnabled)
  const reminders = useQuery('review-reminder-candidates', signal => api.reminderCandidates(signal), reminderEnabled)
  return {
    data: computed(() => main.data.value ? {
      items: main.data.value.items, nextCursor: main.data.value.next_cursor,
      voiceCandidates: voice.data.value?.items ?? [], reminderCandidates: reminders.data.value?.items ?? [],
    } : null),
    loading: main.loading, error: main.error,
    remindersReady: computed(() => !reminders.error.value && !reminders.loading.value && Boolean(reminders.data.value)),
    candidateErrors: computed(() => [voice.error.value, reminders.error.value].filter(Boolean)),
    async refresh(force = false) {
      await main.refresh(force)
      await Promise.all([voiceEnabled() ? voice.refresh(force) : undefined, reminderEnabled() ? reminders.refresh(force) : undefined])
    },
    async retryCandidates() { await Promise.all([voiceEnabled() ? voice.refresh(true) : undefined, reminderEnabled() ? reminders.refresh(true) : undefined]) },
  }
}

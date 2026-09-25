import type { SelfIdentity } from './recording_types'

export interface ReminderEvidenceSpan {
  evidence_span_id: string
  utterance_id: string
  media_id: string
  text: string
  identity: SelfIdentity
  asset_start_ms: number
  asset_end_ms: number
  session_start_ms: number
  session_end_ms: number
}

export type ReminderCandidateStatus =
  | 'pending_confirmation'
  | 'auto_applied'
  | 'confirmed'
  | 'modified'
  | 'ignored'
  | 'duplicate'
  | 'conflict'

export interface ReminderCandidate {
  candidate_id: string
  proposal_id: string
  generation_id: string
  operation: 'CREATE_TASK' | 'CREATE_APPOINTMENT' | 'UPDATE_EVENT' | 'CANCEL_EVENT' | 'MARK_DONE' | 'IGNORE'
  session_id: string
  title: string | null
  actor_person_id: string
  commitment_direction: 'self_to_other' | 'other_to_self' | 'mutual' | 'not_applicable'
  related_person_ids: string[]
  scheduled_at: string | null
  location: string | null
  confidence: number
  needs_confirmation: boolean
  target_event_id: string | null
  expected_revision: number
  status: ReminderCandidateStatus
  proposal_status: 'pending' | 'accepted' | 'rejected'
  matched_event_id: string | null
  conflict_reason: string | null
  created_at: string
  evidence_utterance_ids: string[]
  evidence: ReminderEvidenceSpan[]
}

export interface ReminderSchedule {
  event_id: string
  session_id: string
  event_revision: number
  source_candidate_id: string
  title: string
  actor_person_id: string
  commitment_direction: ReminderCandidate['commitment_direction']
  related_person_ids: string[]
  scheduled_at: string
  location: string | null
  status: 'scheduled' | 'delivered' | 'completed' | 'cancelled' | 'stale'
  delivered_at: string | null
  event_status: string
  source_review_required?: boolean
}

export interface CodexReminderGeneration {
  generation_id: string
  status: 'succeeded'
  candidates: ReminderCandidate[]
  codex: {
    turn_id: string
    model: string
    reasoning_effort: 'low' | 'medium' | 'high' | 'xhigh'
    usage: Record<string, unknown>
  }
}

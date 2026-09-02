import type { ReminderSchedule } from './reminder_types'

export interface PersonSummary {
  person_id: string
  display_name: string
  kind: 'self' | 'known'
  user_confirmed: 0 | 1
  revision: number
  cluster_count: number
  prototype_count: number
  memory_count: number
  interaction_count: number
  last_interaction_at: string | null
  enrollment_reference_count: number
  auto_identity_enabled: boolean
  identity_policy_version: string | null
  voice_policy_revision: number | null
  voice_maturity_status: 'seed' | 'learning' | 'calibrated' | 'suspended' | null
  known_auto_match_enabled: boolean
  suggest_threshold: number | null
  auto_accept_threshold: number | null
  minimum_margin: number | null
  minimum_quality: number | null
  voice_calibration: {
    positive_count?: number
    positive_session_count?: number
    negative_count?: number
    positive_recall?: number
    false_accept_rate?: number
  }
  rejected_prototype_count: number
  pending_voice_review_count: number
  training_voice_review_count: number
}

export type PersonMemoryKind = 'stable_fact' | 'preference' | 'short_term_state' | 'plan' | 'commitment' | 'model_observation'

export type PersonMemoryStatus = 'active' | 'expired' | 'retracted'

export interface PersonMemoryEvidence {
  event_id: string | null
  event_revision: number | null
  utterance_id: string | null
  session_id: string | null
  start_at: string | null
  end_at: string | null
  start_ms: number | null
  end_ms: number | null
  session_start_ms: number | null
  session_end_ms: number | null
  evidence_span_id: string | null
  asset_id: string | null
  text: string | null
  media_id: string | null
}

export interface PersonMemoryActions {
  can_revise: boolean
  can_expire: boolean
  can_retract: boolean
  can_undo: boolean
}

export interface PersonMemory {
  memory_id: string
  revision: number
  person_id: string
  kind: PersonMemoryKind
  summary: string
  details: Record<string, unknown> & { topics?: string[]; commitment_direction?: string | null }
  source: 'human' | 'event_projection' | 'model'
  confidence: number
  confirmation_status: 'confirmed' | 'unconfirmed' | 'inferred'
  valid_from: string
  valid_until: string | null
  status: PersonMemoryStatus
  event_id: string | null
  reminder_event_id: string | null
  reminder: ReminderSchedule | null
  evidence: PersonMemoryEvidence[]
  available_actions: PersonMemoryActions
}

export interface PersonInteraction {
  interaction_type: 'encounter' | 'event'
  session_id: string
  occurred_at: string
  title: string
  event_id: string | null
  event_kind: string | null
  status?: string
  utterance_count?: number
  evidence_utterance_ids?: string[]
}

export interface PersonDetail extends PersonSummary {
  profile_revision: number
  aliases: string[]
  relationship_labels: string[]
  notes: string
  first_seen_at: string | null
  last_seen_at: string | null
  interactions: PersonInteraction[]
  memories: PersonMemory[]
  commitments: PersonMemory[]
  topics: Array<{ label: string; count: number }>
}

export interface SpeakerClusterMember {
  membership_id: string
  speaker_track_id: string
  session_id: string
  label: string
  source: 'automatic' | 'human'
  confidence: number
}

export interface RepresentativeClip {
  media_id: string
  start_ms: number
  end_ms: number
  utterance_id: string | null
}

export interface SpeakerPrototype {
  prototype_id: string
  speaker_track_id: string
  status: 'candidate' | 'accepted'
  quality_score: number
  representative_clips: RepresentativeClip[]
  created_at: string
  decision_tier: SpeakerMatchTier | null
  candidate_person_id: string | null
  best_score: number | null
  second_best_score: number | null
  score_margin: number | null
  match_reason: string | null
  reviewed_person_id: string | null
  review_status: VoicePrototypeReviewStatus | null
  review_note: string | null
}

export type SpeakerMatchTier = 'insufficient_evidence' | 'auto_matched' | 'suggested' | 'no_known_match'

export type VoicePrototypeReviewStatus = 'pending' | 'confirmed' | 'rejected' | 'uncertain' | 'retracted'

export interface VoicePrototypeCandidate {
  prototype_id: string
  speaker_track_id: string
  cluster_id: string
  session_id: string
  quality_score: number
  person_id: string
  person_name: string
  decision_tier: SpeakerMatchTier | null
  best_score: number | null
  second_best_score: number | null
  score_margin: number | null
  match_reason: string | null
  review_status: VoicePrototypeReviewStatus
  representative_clips: RepresentativeClip[]
  created_at: string
}

export interface SpeakerCluster {
  cluster_id: string
  display_label: string
  status: 'active' | 'merged' | 'split' | 'ignored'
  revision: number
  person_id: string | null
  person_name: string | null
  suggested_person_name: string | null
  suggested_person_id: string | null
  suggestion_confidence: number | null
  track_count: number
  session_count: number
  latest_session_id: string | null
  session_ids: string[]
  link_source: 'human' | 'automatic' | null
  link_confidence: number | null
  members?: SpeakerClusterMember[]
  prototypes?: SpeakerPrototype[]
  operations?: Array<Record<string, unknown>>
}

export interface SpeakerAnalysisResult {
  cluster_run_id: string
  session_id: string
  status: 'succeeded'
  track_count: number
  embedded_track_count: number
  new_cluster_count: number
  matched_track_count: number
  person_suggestion_count: number
  rematched_prototype_count: number
  suggested_cluster_count: number
  auto_identified_known_cluster_count: number
  auto_known_identity_updated_utterance_count: number
  auto_identified_self_cluster_count: number
  auto_identity_updated_utterance_count: number
  unusable_track_ids: string[]
}

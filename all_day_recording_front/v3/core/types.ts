export interface SessionSummary {
  session_id: string
  captured_start: string
  captured_end: string | null
  timezone: string
  state: string
  revision: number
  status_code: string
  current_stage: string | null
  progress: number
  updated_at: string
  blocking_reason: string | null
  segment_count: number
  duration_ms: number
  media_id: string | null
  latest_run_id: string | null
  processing_status: string | null
}

export interface ProcessingJobSummary {
  job_id: string
  run_id: string
  session_id: string
  pipeline_version: string
  revision: number
  status: string
  current_stage: string | null
  progress: number
  stage_count: number
  completed_stage_count: number
  error: string | null
  created_at: string
  updated_at: string
}

export interface Overview {
  counts: {
    sessions: number
    active_jobs: number
    retryable_jobs: number
    active_devices: number
    audio_bytes: number
    backed_up_sessions: number
  }
  recent_sessions: SessionSummary[]
  active_jobs: ProcessingJobSummary[]
}

export interface ApiErrorBody {
  code: string
  message: string
  details: Record<string, unknown>
  request_id: string
}

export interface SessionPage {
  items: SessionSummary[]
  next_cursor: string | null
}

export interface SegmentSummary {
  segment_id: string
  sequence: number
  session_start_ms: number
  session_end_ms: number
  source_start_ms: number
  source_end_ms: number
  asset_id: string
  media_id: string
  sha256: string
  size_bytes: number
  duration_ms: number
  format: string
  replica_state: string
  device_name: string | null
}

export interface RunSummary {
  run_id: string
  job_id: string | null
  session_id: string
  pipeline_version: string
  input_revision: number
  revision: number
  status: string
  job_status: string | null
  current_stage: string | null
  progress: number
  error: string | null
  created_at: string
  updated_at: string
  completed_at: string | null
}

export interface Utterance {
  utterance_id: string
  session_id: string
  speaker_track_id: string | null
  speaker_label: string | null
  original_speaker_track_id: string | null
  original_speaker_label: string | null
  identity: SelfIdentity
  original_identity: SelfIdentity
  identity_evidence: Record<string, unknown>
  start_ms: number
  end_ms: number
  start_at: string
  end_at: string
  text: string
  original_text: string
  revision: number
  status: 'active' | 'stale'
  evidence: Record<string, unknown>
}

export type SelfIdentity = 'self' | 'not_self' | 'unknown'

export interface SpeakerTrackSummary {
  speaker_track_id: string
  session_id: string
  label: string
  source_artifact_id: string
  created_at: string
  speaker_cluster_id: string | null
  person_id: string | null
  person_name: string | null
}

export interface ArtifactSummary {
  artifact_id: string
  run_id: string
  kind: string
  producer: string
  producer_version: string
  status: string
  sha256: string | null
  size_bytes: number | null
  metadata: Record<string, unknown>
  created_at: string
}

export interface BackupSummary {
  evidence_id: string
  provider: string
  storage_kind: string
  digest: string
  status: string
  restore_checked_at: string | null
  metadata: Record<string, unknown>
  created_at: string
}

export interface SessionDetail {
  session: SessionSummary
  segments: SegmentSummary[]
  runs: RunSummary[]
  speaker_tracks: SpeakerTrackSummary[]
  utterances: Utterance[]
  artifacts: ArtifactSummary[]
  backups: BackupSummary[]
}

export interface StageSummary {
  stage_run_id: string
  stage: string
  ordinal: number
  optional: boolean
  status: string
  progress: number
  output: Record<string, unknown> | null
  error: string | null
}

export interface AttemptSummary {
  attempt_id: string
  stage_run_id: string
  attempt_number: number
  status: string
  worker_id: string
  config: Record<string, unknown>
  checkpoint: Record<string, unknown> | null
  log_summary: string | null
  error: string | null
  started_at: string
  completed_at: string | null
}

export interface ProcessingSnapshot {
  job: ProcessingJobSummary
  run: RunSummary
  stages: StageSummary[]
  attempts: AttemptSummary[]
}

export interface ReviewItem {
  run_id: string
  session_id: string
  stage_run_id: string
  stage: string
  status: string
  error: string | null
  updated_at: string
}

export interface DeviceSummary {
  device_id: string
  kind: string
  name: string
  status: string
  revision: number
  last_seen_at: string | null
  updated_at: string
  paired: boolean
  receiver_id: string | null
}

export interface DataHealth {
  audio_assets: number
  audio_bytes: number
  available_replicas: number
  unhealthy_replicas: number
  verified_backups: number
  artifacts: number
  artifact_bytes: number
  stale_artifacts: number
}

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
  text: string | null
  media_id: string | null
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
}

export interface SpeakerCluster {
  cluster_id: string
  display_label: string
  status: 'active' | 'merged' | 'split' | 'ignored'
  revision: number
  person_id: string | null
  person_name: string | null
  suggested_person_id: string | null
  suggestion_confidence: number | null
  track_count: number
  session_count: number
  latest_session_id: string | null
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
  unusable_track_ids: string[]
}

export interface InsightEvidence {
  event_id: string | null
  event_revision: number | null
  utterance_id: string | null
  utterance_revision: number | null
  text?: string
  media_id?: string | null
  start_ms?: number | null
  end_ms?: number | null
  start_at?: string
}

export interface InsightNarrativeItem {
  text: string
  evidence_event_ids: string[]
  evidence_utterance_ids: string[]
}

export interface DailySummary {
  summary_id: string
  revision: number
  summary_date: string
  timezone: string
  objective: {
    statistics: Record<string, number | Record<string, number>>
    decisions: Array<Record<string, unknown>>
    new_todos: Array<Record<string, unknown>>
    completed: Array<Record<string, unknown>>
    unresolved: Array<Record<string, unknown>>
    people_interactions: Array<Record<string, unknown>>
    key_quotes: Array<Record<string, unknown>>
  }
  narrative: Record<string, InsightNarrativeItem[]>
  input_sha256: string
  provenance: Record<string, unknown> & { reasoning_effort?: string; model?: string }
  status: 'active' | 'retracted'
  derivation_status: 'active' | 'retracted' | 'stale'
  created_at: string
  evidence: InsightEvidence[]
}

export interface RelationshipObservation extends InsightNarrativeItem {
  confidence: number
  rationale: string
}

export interface RelationshipReport {
  report_id: string
  revision: number
  person_id: string
  window_days: 7 | 30
  end_date: string
  timezone: string
  verified_facts: Record<string, unknown> & {
    interaction_frequency?: Record<string, number>
    topics?: Record<string, Array<{ label: string; count: number }>>
    unfinished_commitments?: Array<Record<string, unknown>>
    last_contact_at?: string | null
    days_since_contact?: number | null
  }
  observations: RelationshipObservation[]
  input_sha256: string
  provenance: Record<string, unknown> & { reasoning_effort?: string; model?: string }
  status: 'active' | 'retracted'
  derivation_status: 'active' | 'retracted' | 'stale'
  created_by: string
  created_at: string
  evidence: InsightEvidence[]
}

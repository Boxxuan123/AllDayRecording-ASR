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

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

import type { SessionSummary } from './recording_types'
import type { ProcessingJobSummary } from './processing_types'

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

export type ReviewKind = 'processing_gate' | 'reminder' | 'person_memory' | 'voice_identity'

export type ReviewPriority = 'high' | 'normal'

export interface ReviewItem {
  review_id: string
  kind: ReviewKind
  priority: ReviewPriority
  source_id: string
  source_revision: number | null
  session_id: string | null
  person_id: string | null
  title: string
  summary: string
  reason: string
  evidence_count: number
  created_at: string
  updated_at: string
  context: Record<string, string | number | boolean | null | undefined>
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

export interface DesktopSettings {
  contract_version: string
  deployment: string
  processing: {
    durable_jobs: boolean
    backup_admission_required: boolean
    automatic_source_deletion: boolean
  }
  privacy: {
    network_boundary: string
    audio_cloud_upload: boolean
    transcript_cloud_processing: boolean
  }
  reminders: {
    codex_enabled: boolean
    codex_workspace: string
    model_write_boundary: string
  }
  insights: {
    codex_enabled: boolean
    codex_workspace: string
    source_layer: string
    relationship_windows_days: number[]
    versioned_corrections: boolean
  }
}

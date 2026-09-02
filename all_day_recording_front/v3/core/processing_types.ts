import type { RunSummary } from './recording_types'

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

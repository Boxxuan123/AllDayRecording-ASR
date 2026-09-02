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

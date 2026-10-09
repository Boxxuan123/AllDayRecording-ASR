import { request } from './api'

export interface ChatRecord {
  record_id: string; record_revision: number; platform: string
  source_account_id: string; conversation_id: string; sender_account_id: string | null
  sent_at: number | null; text: string | null; text_status?: string; message_type?: string
  sender_display_name?: string | null; conversation_display_name?: string | null
  attachments?: Record<string, unknown>[]; source_locator: Record<string, unknown>
  reply?: unknown; quote?: unknown; forward?: unknown
}
export interface ChatScope {
  platform?: string; source_account_id?: string; conversation_id?: string
  sender_account_id?: string; sent_from?: number; sent_to?: number; keyword?: string
}
export interface ChatPage {
  items: ChatRecord[]; next_cursor: string | null; has_more: boolean; origin: string
  scope: ChatScope; complete?: boolean; source_error?: string; historical_lookup?: boolean
}
export interface ChatContext {
  items: ChatRecord[]; references: Record<string, unknown>[]; reference_status: string
  origin: string; truncated_before: boolean; truncated_after: boolean; anchor_record_id: string
}
export interface ChatStatus {
  connection: { base_url: string; configured: boolean }
  source: Record<string, unknown> | null; coverage: Record<string, unknown> | null
  error: string | null; last_contact_at: number | null; paused: boolean; source_limitations: string
  cache: { received: number; indexed: number; pending: number; failed: number; processing_watermark: number }
  scopes: { id: string; scope: ChatScope; received: number; phase: string; error: string | null; updated_at: number; enabled: number }[]
}
export interface ChatAnswer {
  claims: { text: string; kind: string; citations: string[] }[]; unknown: string[]
  evidence: ChatRecord[]; disclaimer?: string; known_gaps?: string[]; generation: number
}
export interface ChatJob { id: string; state: string; error?: string; value: ChatAnswer | null; stale?: boolean }
export interface DirectoryPage { items: Record<string, unknown>[]; next_cursor: string | null; has_more: boolean; origin: string }
const get = <T>(path: string, params: Record<string, unknown> = {}) => {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== null) query.set(key, typeof value === 'object' ? JSON.stringify(value) : String(value))
  return request<T>(`/api/v3/chat/${path}?${query}`)
}
const post = <T>(path: string, body: unknown) => request<T>(`/api/v3/chat/${path}`, { method: 'POST', body: JSON.stringify(body), signal: AbortSignal.timeout(65_000) })
export const chatApi = {
  configure: (baseUrl: string, token: string) => post('connection', { base_url: baseUrl, token }),
  status: () => get<ChatStatus>('status'),
  directory: (kind: string, scope: ChatScope, cursor?: string | null) => get<DirectoryPage>(kind, { scope, cursor }),
  search: (scope: ChatScope, cursor?: string | null, origin = 'mac') => get<ChatPage>('messages', { scope, cursor, origin }),
  context: (recordId: string, size = 10) => get<ChatContext>('context', { record_id: recordId, before: size, after: size }),
  sync: (scope: ChatScope, reinitialize = false) => post('sync', { scopes: [scope], reinitialize }),
  control: (action: string, scopeId?: string) => post('control', { action, scope_id: scopeId }),
  answer: (question: string, scope: ChatScope, origin = 'mac') => post<ChatJob>('answer', { question, scope, origin }),
  job: (id: string) => get<ChatJob>('jobs', { id }),
  cancel: (id: string) => post('cancel', { id }),
}

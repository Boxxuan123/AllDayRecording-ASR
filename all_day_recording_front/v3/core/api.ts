import {
  mockDataHealth,
  mockDevices,
  mockOverview,
  mockProcessingSnapshot,
  mockSessionDetail,
} from './mock'
import type {
  ApiErrorBody,
  DataHealth,
  DeviceSummary,
  Overview,
  ProcessingJobSummary,
  ProcessingSnapshot,
  ReviewItem,
  SessionDetail,
  SessionPage,
  Utterance,
} from './types'

const mock = import.meta.env.VITE_V3_MOCK === '1'

export class ApiError extends Error {
  readonly code: string
  readonly requestId: string

  constructor(
    message: string,
    code: string,
    requestId: string,
  ) {
    super(message)
    this.code = code
    this.requestId = requestId
  }
}

export const IS_MOCK = mock

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (mock) return mockRequest<T>(path, init)
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({
      code: 'http_error',
      message: `请求失败：${response.status}`,
      request_id: '',
      details: {},
    }))) as ApiErrorBody
    throw new ApiError(payload.message, payload.code, payload.request_id)
  }
  return response.json() as Promise<T>
}

export const desktopApi = {
  overview: () => request<Overview>('/api/v3/overview'),
  sessions: () => request<SessionPage>('/api/v3/recording-sessions?limit=100'),
  session: (sessionId: string) => request<SessionDetail>(`/api/v3/recording-sessions/${encodeURIComponent(sessionId)}`),
  processingJobs: (status = '') => request<{ items: ProcessingJobSummary[] }>(`/api/v3/processing-jobs?limit=100${status ? `&status=${encodeURIComponent(status)}` : ''}`),
  processingJob: (jobId: string) => request<ProcessingSnapshot>(`/api/v3/processing-jobs/${encodeURIComponent(jobId)}`),
  retryJob: (jobId: string) => request<ProcessingSnapshot>(`/api/v3/processing-jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST', body: '{}' }),
  cancelJob: (jobId: string, reason: string) => request<ProcessingSnapshot>(`/api/v3/processing-jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', body: JSON.stringify({ reason }) }),
  reviews: () => request<{ items: ReviewItem[] }>('/api/v3/reviews?limit=100'),
  devices: () => request<{ items: DeviceSummary[] }>('/api/v3/devices'),
  dataHealth: () => request<DataHealth>('/api/v3/data-health'),
  settings: () => request<Record<string, unknown>>('/api/v3/settings'),
  lab: () => request<{ enabled: boolean; label: string; message: string }>('/api/v3/lab'),
  correctUtterance: (utteranceId: string, expectedRevision: number, text: string) => request<Utterance>(`/api/v3/utterances/${encodeURIComponent(utteranceId)}/corrections`, { method: 'POST', body: JSON.stringify({ expected_revision: expectedRevision, text }) }),
}

async function mockRequest<T>(path: string, init?: RequestInit): Promise<T> {
  await Promise.resolve()
  if (path === '/api/v3/overview') return mockOverview as T
  if (path.startsWith('/api/v3/recording-sessions?')) return { items: mockOverview.recent_sessions, next_cursor: null } as T
  if (path.startsWith('/api/v3/recording-sessions/')) return structuredClone(mockSessionDetail) as T
  if (path.startsWith('/api/v3/processing-jobs?')) return { items: mockOverview.active_jobs } as T
  if (/\/processing-jobs\/[^/]+\/(retry|cancel)$/.test(path)) return mockProcessingSnapshot as T
  if (/\/processing-jobs\/[^/]+$/.test(path)) return mockProcessingSnapshot as T
  if (path.startsWith('/api/v3/reviews')) return { items: [] } as T
  if (path === '/api/v3/devices') return { items: mockDevices } as T
  if (path === '/api/v3/data-health') return mockDataHealth as T
  if (path === '/api/v3/settings') return { contract_version: '3.0.0', deployment: 'local_only', processing: { durable_jobs: true, backup_admission_required: true, automatic_source_deletion: false }, privacy: { network_boundary: 'loopback', cloud_upload: false } } as T
  if (path === '/api/v3/lab') return { enabled: false, label: '实验室', message: '实验与 benchmark 工具保留在独立 Legacy/Lab 入口。' } as T
  if (path.includes('/corrections') && init?.body) {
    const utteranceId = decodeURIComponent(path.split('/')[4])
    const original = mockSessionDetail.utterances.find(
      (item) => item.utterance_id === utteranceId,
    )
    if (!original) throw new ApiError('utterance 不存在', 'not_found', 'mock')
    return {
      ...original,
      revision: original.revision + 1,
      text: JSON.parse(String(init.body)).text,
    } as T
  }
  throw new ApiError(`mock route not found: ${path}`, 'not_found', 'mock')
}

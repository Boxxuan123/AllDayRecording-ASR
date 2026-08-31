// Generated enum snapshots from contracts/v3/schemas/primitives.schema.json.
// The repository contract tests guard the canonical values; this client keeps
// unknown wire values renderable instead of throwing during an upgrade.
export const UNKNOWN_ENUM_VALUE = 'unknown'

const knownEnums = {
  recordingSessionState: new Set([
    'capturing',
    'closing',
    'recovering',
    'quarantined',
    'sealed',
    'phone_verified',
    'computer_ingested',
    'admission_pending',
    'admission_blocked',
    'ready_for_processing',
  ]),
  aggregateStatus: new Set([
    'awaiting_upload',
    'verifying',
    'backup_required',
    'ready',
    'processing',
    'needs_review',
    'available',
    'failed',
    'stale',
  ]),
  processingStatus: new Set([
    'created',
    'queued',
    'running',
    'waiting_review',
    'succeeded',
    'failed_retryable',
    'failed_final',
    'cancel_requested',
    'cancelled',
    'stale',
  ]),
  receiptStatus: new Set(['pending', 'applied', 'conflict', 'rejected']),
  resourceType: new Set([
    'recording_session',
    'audio_asset',
    'processing_run',
    'utterance',
    'review_item',
  ]),
  syncOperation: new Set(['upsert', 'tombstone']),
}

export function normalizeV3Enum(value, enumName) {
  return knownEnums[enumName].has(value) ? value : UNKNOWN_ENUM_VALUE
}

export function decodeV3ContractFixture(payload) {
  const receipt = payload.device_sync_response.receipts[0]
  const change = payload.device_sync_response.changes[0]
  return {
    contract_version: payload.contract_version,
    session_state: normalizeV3Enum(
      payload.recording_session.state,
      'recordingSessionState',
    ),
    aggregate_status: normalizeV3Enum(
      payload.recording_session.status_code,
      'aggregateStatus',
    ),
    processing_status: normalizeV3Enum(
      payload.processing_run.status,
      'processingStatus',
    ),
    receipt_status: normalizeV3Enum(receipt.status, 'receiptStatus'),
    change_resource_type: normalizeV3Enum(
      change.resource_type,
      'resourceType',
    ),
    change_operation: normalizeV3Enum(change.operation, 'syncOperation'),
    next_cursor:
      typeof payload.device_sync_response.next_cursor === 'string'
        ? payload.device_sync_response.next_cursor
        : null,
  }
}

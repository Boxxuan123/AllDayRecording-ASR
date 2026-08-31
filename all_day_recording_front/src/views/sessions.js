import { state } from '../state/workspace.js'
import {
  formatDuration,
  formatRecordingTime,
  workflowStateLabel,
} from '../utils/format.js'
import { $, node } from '../workspace/dom.js'

export function sessionOptionLabel(session) {
  const recordedAt = formatRecordingTime(session.recorded_at, session.timezone)
  const stateLabel = workflowStateLabel(session.workflow_state)
  return `${recordedAt} · ${formatDuration(session.duration_ms)} · S${session.id} · ${stateLabel}`
}

export function renderSessionOptions() {
  const select = $('#recording-select')
  select.replaceChildren()
  const sessions = [...state.sessions].sort(
    (left, right) => new Date(right.recorded_at) - new Date(left.recorded_at),
  )
  sessions.forEach((session) => {
    const option = node('option', '', sessionOptionLabel(session))
    option.value = session.id
    option.title = session.source_name
    select.append(option)
  })
}

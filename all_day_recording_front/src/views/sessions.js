import { state } from '../state/workspace.js'
import { formatDuration, workflowStateLabel } from '../utils/format.js'
import { $, node } from '../workspace/dom.js'

export function renderSessionOptions() {
  const select = $('#recording-select')
  select.replaceChildren()
  state.sessions.forEach((session) => {
    const stateLabel = ` · ${workflowStateLabel(session.workflow_state)}`
    const option = node(
      'option',
      '',
      `S${session.id} · ${session.source_name} · ${formatDuration(session.duration_ms)}${stateLabel}`,
    )
    option.value = session.id
    select.append(option)
  })
}

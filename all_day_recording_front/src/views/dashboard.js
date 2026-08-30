import { state } from '../state/workspace.js'
import { workflowStateLabel } from '../utils/format.js'
import { $ } from '../workspace/dom.js'

export function renderDashboard() {
  const dashboard = state.dashboard;
  const asr = dashboard?.asr?.summary || {};
  const diarization = dashboard?.diarization?.summary || {};
  const workflow = dashboard?.workflow?.summary || {};
  const integrity = dashboard?.integrity || {};
  $("#metric-segments").textContent = Number(asr.committed_primary_tokens || 0).toLocaleString("zh-CN");
  $("#metric-segments-detail").textContent = dashboard?.asr?.available
    ? `${asr.accepted_speech_candidates || 0}/${asr.speech_candidates || 0} 语音候选 · run #${dashboard.asr.id}`
    : "尚未完成 V2-C";
  $("#metric-annotations").textContent = String(diarization.speakers || 0);
  $("#metric-annotations-detail").textContent = dashboard?.diarization?.available
    ? `${diarization.regular_turns || 0} turns · ${diarization.overlap_regions || 0} 重叠区间`
    : "尚未完成 V2-D";
  $("#metric-evaluation").textContent = workflowStateLabel(workflow.workflow_state);
  $("#metric-evaluation-detail").textContent = dashboard?.workflow?.available
    ? `workflow #${dashboard.workflow.id} · ${dashboard.workflow.status}`
      + (workflow.review?.required ? ` · 人工检查 ${workflow.review.reasons?.length || 0}` : "")
    : "等待 V2 工作流";
  $("#metric-actions").textContent = integrity.available
    ? `${integrity.verified_instances}/${integrity.instance_count}`
    : `${dashboard?.session?.chunk_count || 0} 分片`;
  $("#metric-actions-detail").textContent = integrity.available
    ? `原音已校验 · gap ${integrity.gaps} · overlap ${integrity.overlaps}`
    : "尚无工作流完整性快照";
  const badge = $("#pending-action-badge");
  badge.textContent = String(state.actions.length);
  badge.classList.toggle("hidden", state.actions.length === 0);
}

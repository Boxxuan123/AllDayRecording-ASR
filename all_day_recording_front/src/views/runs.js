import { state } from '../state/workspace.js'
import { formatDate, formatDuration } from '../utils/format.js'
import { $, node } from '../workspace/dom.js'

export function renderRuns() {
  const body = $("#run-table-body");
  body.replaceChildren();
  state.runs.forEach((run) => {
    const row = node("tr");
    row.append(node("td", "", `#${run.id}`));
    row.append(node("td", "run-kind", runKindLabel(run.run_kind)));
    const statusCell = node("td");
    const statusClass = run.status === "completed" ? "confirmed" : run.status === "failed" ? "dismissed" : "pending";
    statusCell.append(node("span", `status-chip ${statusClass}`, run.status));
    row.append(statusCell);
    row.append(node("td", "", formatDate(run.started_at)));
    row.append(node("td", "mono-cell", (run.config_sha256 || "").slice(0, 12) || "—"));
    row.append(node("td", "run-summary-cell", summarizeRun(run)));
    body.append(row);
  });
  if (!state.runs.length) {
    const row = node("tr");
    const cell = node("td", "", "当前会话还没有 V2 运行记录");
    cell.colSpan = 6;
    row.append(cell);
    body.append(row);
  }
}
function runKindLabel(kind) {
  return {
    quality_workflow_v2: "V2 Workflow",
    quality_asr_v2c: "V2-C ASR",
    quality_diarization_v2d: "V2-D 说话人",
    semantic_v2e0: "V2-E 语义",
    quality_diarization_v2d1: "V2-D.1 召回",
    quality_diarization_v2d2: "V2-D.2 审计",
    quality_diarization_v2d3: "V2-D.3 身份",
  }[kind] || kind;
}

function summarizeRun(run) {
  const summary = run.summary || {};
  if (run.error) return run.error;
  if (run.run_kind === "quality_workflow_v2") {
    return `${summary.workflow_state || "—"} · ${summary.admission_mode || "—"}`;
  }
  if (run.run_kind === "quality_asr_v2c") {
    return `${summary.committed_primary_tokens || 0} tokens · ${summary.window_count || 0} 窗口 · ${summary.disagreements || 0} 分歧`;
  }
  if (run.run_kind === "quality_diarization_v2d") {
    return `${summary.speakers || 0} 人 · ${summary.regular_turns || 0} turns · ${formatDuration(summary.overlap_ms || 0)} 重叠`;
  }
  if (run.run_kind === "semantic_v2e0") {
    return `${summary.episode_count || 0} episode · ${summary.scene_count || 0} scene · ${summary.provider || "local"}`;
  }
  return Object.keys(summary).slice(0, 3).map((key) => `${key}=${String(summary[key])}`).join(" · ") || "—";
}

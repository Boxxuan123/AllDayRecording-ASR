import { api } from '../api/client.js'
import {
  installExclusiveAudioPlayback,
  pauseAllAudio,
  releaseAudioWithin,
} from '../audio/playback.js'
import {
  resetSessionWorkspace,
  sessionQuery,
  state,
} from '../state/workspace.js'
import { renderActions } from '../views/actions.js'
import { renderDashboard } from '../views/dashboard.js'
import {
  loadEvaluation,
  renderEvaluationOptions,
  renderEvaluationSegments,
  renderSessionEvaluation,
  runEvaluation,
} from '../views/evaluation.js'
import { renderRuns } from '../views/runs.js'
import { generateSemantic, renderSemantic } from '../views/semantic.js'
import { renderSessionOptions } from '../views/sessions.js'
import {
  renderTimelineOverview,
  selectTimelineQueue,
  stopTimelineAudioPlayback,
} from '../views/timeline.js'
import { $, $$, toast } from './dom.js'
import { setWorkspaceReloader } from './reload.js'

export async function initializeWorkbench() {
  installExclusiveAudioPlayback();
  setWorkspaceReloader(loadSessionWorkspace);
  bindNavigation();
  bindToolbar();
  try {
    const payload = await api("/api/sessions");
    state.sessions = payload.sessions;
    renderSessionOptions();
    if (!state.sessions.length) {
      toast("数据库中还没有录音会话，请先导入音频。", "error");
      return;
    }
    selectSession(state.sessions[0].id);
    $("#recording-select").value = String(state.sessionId);
    await loadSessionWorkspace();
  } catch (error) {
    toast(error.message, "error");
  }
}

function bindNavigation() {
  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  $$(".filter-button").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      state.page = 1;
      $$(".filter-button").forEach((item) => item.classList.toggle("active", item === button));
      renderEvaluationSegments();
    });
  });
  $$(".timeline-queue-button").forEach((button) => {
    button.addEventListener("click", () => selectTimelineQueue(button.dataset.timelineQueue));
  });
}

function bindToolbar() {
  $("#recording-select").addEventListener("change", async (event) => {
    stopTimelineAudioPlayback();
    releaseAudioWithin(document);
    selectSession(Number(event.target.value));
    resetSessionWorkspace();
    renderWorkflowButton();
    await loadSessionWorkspace();
  });
  $("#evaluation-select").addEventListener("change", async (event) => {
    state.evaluationName = event.target.value || null;
    state.page = 1;
    await loadEvaluation();
  });
  $("#run-evaluation-button").addEventListener("click", runEvaluation);
  $("#run-workflow-button").addEventListener("click", startQualityWorkflow);
  $("#refresh-workspace-button").addEventListener("click", refreshWorkspace);
  $("#generate-semantic-button").addEventListener("click", generateSemantic);
}

function switchView(view) {
  stopTimelineAudioPlayback(undefined, false);
  pauseAllAudio();
  state.activeView = view;
  const titles = {
    timeline: "说话人时间轴",
    evaluation: "评测标注",
    semantic: "语义证据",
    actions: "行动候选",
    runs: "运行记录",
  };
  $("#page-title").textContent = titles[view] || "日记工作台";
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $$(".view").forEach((item) => item.classList.toggle("active-view", item.id === `view-${view}`));
}

function selectSession(sessionId) {
  state.sessionId = sessionId;
  state.recordingId = state.sessions.find((item) => item.id === sessionId)?.recording_id || null;
}

async function loadSessionWorkspace() {
  if (!state.sessionId) return;
  try {
    const [dashboard, runs, timeline, semantic, sessionEvaluation] = await Promise.all([
      api(`/api/session-dashboard?${sessionQuery()}`),
      api(`/api/runs?${sessionQuery()}`),
      api(`/api/speaker-timeline?${sessionQuery()}`),
      api(`/api/semantic?${sessionQuery()}`),
      api(`/api/session-evaluation?${sessionQuery()}`),
    ]);
    let templates = { evaluations: [] };
    let actions = { actions: [] };
    if (state.recordingId) {
      [templates, actions] = await Promise.all([
        api(`/api/evaluations?recording_id=${state.recordingId}`),
        api(`/api/actions?recording_id=${state.recordingId}`),
      ]);
    }
    state.dashboard = dashboard;
    state.templates = templates.evaluations;
    state.actions = actions.actions;
    state.runs = runs.runs;
    state.timeline = timeline;
    state.semantic = semantic;
    state.sessionEvaluation = sessionEvaluation;
    renderDashboard();
    renderEvaluationOptions();
    renderSessionEvaluation();
    renderActions();
    renderRuns();
    renderTimelineOverview();
    renderSemantic();
    renderWorkflowButton();
    await loadEvaluation();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function refreshWorkspace() {
  const button = $("#refresh-workspace-button");
  stopTimelineAudioPlayback();
  releaseAudioWithin(document);
  button.disabled = true;
  button.textContent = "刷新中…";
  try {
    const payload = await api("/api/sessions");
    const selected = state.sessionId;
    state.sessions = payload.sessions;
    renderSessionOptions();
    const next = state.sessions.some((item) => item.id === selected)
      ? selected
      : state.sessions[0]?.id;
    if (!next) return;
    selectSession(next);
    $("#recording-select").value = String(next);
    await loadSessionWorkspace();
    toast("V2 会话与运行结果已刷新");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "刷新";
  }
}

function renderWorkflowButton() {
  const button = $("#run-workflow-button")
  const session = state.sessions.find((item) => item.id === state.sessionId)
  if (!session || session.kind !== 'manifest') {
    button.disabled = true
    button.textContent = '仅分片会话可启动 V2'
    return
  }
  const databaseRunning = state.dashboard?.workflow?.status === 'running'
  if (state.workflowJobId || databaseRunning) {
    button.disabled = true
    button.textContent = 'V2 运行中…'
    return
  }
  button.disabled = false
  button.textContent = session.workflow_state
    ? '重新运行 V2（Shadow）'
    : '启动 V2（Shadow）'
}

async function startQualityWorkflow() {
  const session = state.sessions.find((item) => item.id === state.sessionId)
  if (!session || session.kind !== 'manifest') return
  const button = $("#run-workflow-button");
  button.disabled = true;
  button.textContent = "正在提交…";
  try {
    const job = await api("/api/workflow-v2", {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId, shadow: true }),
    });
    state.workflowJobId = job.id
    showWorkflowJob(job);
    renderWorkflowButton();
    pollWorkflowJob(job.id);
  } catch (error) {
    state.workflowJobId = null
    renderWorkflowButton();
    toast(error.message, "error");
  }
}

function showWorkflowJob(job) {
  const banner = $("#job-banner");
  banner.classList.remove("hidden");
  $("#job-title").textContent = job.status === "completed"
    ? "V2 工作流已完成"
    : "正在运行 V2 工作流";
  $("#job-detail").textContent = `${job.stage} · ${job.detail}`;
}

function pollWorkflowJob(jobId) {
  window.clearTimeout(state.jobTimer);
  state.jobTimer = window.setTimeout(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      showWorkflowJob(job);
      if (job.status === "completed") {
        state.workflowJobId = null
        toast(`V2 workflow #${job.result.workflow_run_id} 已完成`);
        await refreshWorkspace();
        renderWorkflowButton();
        window.setTimeout(() => $("#job-banner").classList.add("hidden"), 2600);
      } else if (job.status === "failed") {
        state.workflowJobId = null
        renderWorkflowButton();
        toast(job.error || "V2 工作流失败", "error");
      } else {
        pollWorkflowJob(jobId);
      }
    } catch (error) {
      state.workflowJobId = null
      renderWorkflowButton();
      toast(error.message, "error");
    }
  }, 900);
}

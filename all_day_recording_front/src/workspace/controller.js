import { api } from '../api/client.js'
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
import { renderTimelineOverview, selectTimelineQueue } from '../views/timeline.js'
import { $, $$, toast } from './dom.js'
import { setWorkspaceReloader } from './reload.js'

export async function initializeWorkbench() {
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
    selectSession(Number(event.target.value));
    resetSessionWorkspace();
    await loadSessionWorkspace();
  });
  $("#evaluation-select").addEventListener("change", async (event) => {
    state.evaluationName = event.target.value || null;
    state.page = 1;
    await loadEvaluation();
  });
  $("#run-evaluation-button").addEventListener("click", runEvaluation);
  $("#run-daily-button").addEventListener("click", refreshWorkspace);
  $("#generate-semantic-button").addEventListener("click", generateSemantic);
}

function switchView(view) {
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
    await loadEvaluation();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function refreshWorkspace() {
  const button = $("#run-daily-button");
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
    button.textContent = "刷新 V2 结果";
  }
}







async function startDailyRun() {
  if (!state.recordingId) return;
  const button = $("#run-daily-button");
  button.disabled = true;
  try {
    const job = await api("/api/daily-run", {
      method: "POST",
      body: JSON.stringify({ recording_id: state.recordingId }),
    });
    showJob(job);
    pollJob(job.id);
  } catch (error) {
    button.disabled = false;
    toast(error.message, "error");
  }
}

function showJob(job) {
  const banner = $("#job-banner");
  banner.classList.remove("hidden");
  $("#job-title").textContent = job.status === "completed" ? "运行完成" : "正在生成一键日记";
  $("#job-detail").textContent = `${job.stage} · ${job.detail}`;
}

function pollJob(jobId) {
  window.clearTimeout(state.jobTimer);
  state.jobTimer = window.setTimeout(async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      showJob(job);
      if (job.status === "completed") {
        $("#run-daily-button").disabled = false;
        toast(`一键日记 run #${job.result.run_id} 已完成`);
        await loadSessionWorkspace();
        window.setTimeout(() => $("#job-banner").classList.add("hidden"), 2600);
      } else if (job.status === "failed") {
        $("#run-daily-button").disabled = false;
        toast(job.error || "一键运行失败", "error");
      } else {
        pollJob(jobId);
      }
    } catch (error) {
      $("#run-daily-button").disabled = false;
      toast(error.message, "error");
    }
  }, 900);
}

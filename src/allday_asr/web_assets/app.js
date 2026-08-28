"use strict";

const state = {
  recordings: [],
  recordingId: null,
  dashboard: null,
  templates: [],
  evaluationName: null,
  evaluation: null,
  actions: [],
  runs: [],
  filter: "all",
  page: 1,
  pageSize: 8,
  activeView: "evaluation",
  jobTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `请求失败：${response.status}`);
  }
  return payload;
}

function toast(message, type = "success") {
  const item = node("div", `toast ${type === "error" ? "error" : ""}`, message);
  $("#toast-region").append(item);
  window.setTimeout(() => item.remove(), 3600);
}

function formatDuration(milliseconds) {
  const total = Math.max(0, Math.round(milliseconds / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}h ${minutes}m ${seconds}s`;
  return `${minutes}m ${seconds}s`;
}

function formatOffset(milliseconds) {
  const total = Math.max(0, Math.round(milliseconds / 1000));
  const hours = Math.floor(total / 3600).toString().padStart(2, "0");
  const minutes = Math.floor((total % 3600) / 60).toString().padStart(2, "0");
  const seconds = (total % 60).toString().padStart(2, "0");
  return `${hours}:${minutes}:${seconds}`;
}

function formatDate(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function metric(value) {
  return value === null || value === undefined ? "N/A" : Number(value).toFixed(4);
}

async function initialize() {
  bindNavigation();
  bindToolbar();
  try {
    const payload = await api("/api/recordings");
    state.recordings = payload.recordings;
    renderRecordingOptions();
    if (!state.recordings.length) {
      toast("数据库中还没有录音，请先运行 daily-run 导入录音。", "error");
      return;
    }
    state.recordingId = state.recordings[0].id;
    $("#recording-select").value = String(state.recordingId);
    await loadRecordingWorkspace();
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
}

function bindToolbar() {
  $("#recording-select").addEventListener("change", async (event) => {
    state.recordingId = Number(event.target.value);
    state.evaluationName = null;
    state.evaluation = null;
    state.page = 1;
    await loadRecordingWorkspace();
  });
  $("#evaluation-select").addEventListener("change", async (event) => {
    state.evaluationName = event.target.value || null;
    state.page = 1;
    await loadEvaluation();
  });
  $("#run-evaluation-button").addEventListener("click", runEvaluation);
  $("#run-daily-button").addEventListener("click", startDailyRun);
}

function switchView(view) {
  state.activeView = view;
  const titles = { evaluation: "评测标注", actions: "行动候选", runs: "运行记录" };
  $("#page-title").textContent = titles[view] || "日记工作台";
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $$(".view").forEach((item) => item.classList.toggle("active-view", item.id === `view-${view}`));
}

function renderRecordingOptions() {
  const select = $("#recording-select");
  select.replaceChildren();
  state.recordings.forEach((recording) => {
    const option = node(
      "option",
      "",
      `#${recording.id} · ${recording.source_name} · ${formatDuration(recording.duration_ms)}`,
    );
    option.value = recording.id;
    select.append(option);
  });
}

async function loadRecordingWorkspace() {
  if (!state.recordingId) return;
  try {
    const [dashboard, templates, actions, runs] = await Promise.all([
      api(`/api/dashboard?recording_id=${state.recordingId}`),
      api(`/api/evaluations?recording_id=${state.recordingId}`),
      api(`/api/actions?recording_id=${state.recordingId}`),
      api(`/api/runs?recording_id=${state.recordingId}`),
    ]);
    state.dashboard = dashboard;
    state.templates = templates.evaluations;
    state.actions = actions.actions;
    state.runs = runs.runs;
    renderDashboard();
    renderEvaluationOptions();
    renderActions();
    renderRuns();
    await loadEvaluation();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderDashboard() {
  const dashboard = state.dashboard;
  const completed = dashboard?.segments?.completed || 0;
  const total = Object.values(dashboard?.segments || {}).reduce((sum, value) => sum + Number(value), 0);
  $("#metric-segments").textContent = completed.toLocaleString("zh-CN");
  $("#metric-segments-detail").textContent = `${total} 总片段 · ${dashboard.events} 个事件`;
  $("#metric-annotations").textContent = String(dashboard.annotations);
  $("#metric-actions").textContent = String(dashboard.actions.pending);
  const badge = $("#pending-action-badge");
  badge.textContent = String(dashboard.actions.pending);
  badge.classList.toggle("hidden", dashboard.actions.pending === 0);
  updateEvaluationMetric();
}

function renderEvaluationOptions() {
  const select = $("#evaluation-select");
  select.replaceChildren();
  if (!state.templates.length) {
    const option = node("option", "", "暂无评测模板");
    option.value = "";
    select.append(option);
    state.evaluationName = null;
    return;
  }
  state.templates.forEach((template) => {
    const option = node(
      "option",
      "",
      `${template.name} · ${template.text_labeled}/${template.segments}`,
    );
    option.value = template.name;
    select.append(option);
  });
  if (!state.evaluationName || !state.templates.some((item) => item.name === state.evaluationName)) {
    state.evaluationName = state.templates[0].name;
  }
  select.value = state.evaluationName;
}

async function loadEvaluation() {
  const empty = $("#evaluation-empty");
  const list = $("#segment-list");
  if (!state.evaluationName) {
    state.evaluation = null;
    empty.classList.remove("hidden");
    list.replaceChildren();
    $("#pagination").replaceChildren();
    $("#run-evaluation-button").disabled = true;
    updateEvaluationProgress();
    return;
  }
  try {
    state.evaluation = await api(
      `/api/evaluations/${state.recordingId}/${encodeURIComponent(state.evaluationName)}`,
    );
    empty.classList.add("hidden");
    $("#run-evaluation-button").disabled = false;
    renderEvaluationSegments();
    updateEvaluationProgress();
  } catch (error) {
    toast(error.message, "error");
  }
}

function filteredSegments() {
  const segments = state.evaluation?.segments || [];
  if (state.filter === "labeled") return segments.filter(isTextLabeled);
  if (state.filter === "unlabeled") return segments.filter((item) => !isTextLabeled(item));
  return segments;
}

function isTextLabeled(segment) {
  return Boolean((segment.reference_text || "").trim());
}

function renderEvaluationSegments() {
  const container = $("#segment-list");
  container.replaceChildren();
  const rows = filteredSegments();
  const pages = Math.max(1, Math.ceil(rows.length / state.pageSize));
  state.page = Math.min(state.page, pages);
  const pageRows = rows.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
  pageRows.forEach((segment) => container.append(renderSegmentCard(segment)));
  if (!pageRows.length && state.evaluation) {
    const empty = node("div", "empty-state");
    empty.append(node("strong", "", "当前筛选没有片段"));
    empty.append(node("p", "", "切换到“全部”或其他筛选继续。"));
    container.append(empty);
  }
  renderPagination(pages);
}

function renderSegmentCard(segment) {
  const card = node("article", `segment-card ${isTextLabeled(segment) ? "saved" : ""}`);
  const evidence = node("div", "segment-evidence");
  const meta = node("div", "segment-meta");
  meta.append(node("span", "segment-id", `SEGMENT ${segment.segment_id}`));
  meta.append(node("span", "time-chip", `${formatOffset(segment.start_ms)}–${formatOffset(segment.end_ms)}`));
  evidence.append(meta);

  const audio = node("audio");
  audio.controls = true;
  audio.preload = "none";
  audio.src = segment.audio_url;
  audio.setAttribute("aria-label", `播放片段 ${segment.segment_id}`);
  evidence.append(audio);
  evidence.append(node("div", "speaker-chip", segment.hypothesis_speaker_at_export || "unknown"));
  evidence.append(node("p", "hypothesis-label", "系统当前识别"));
  evidence.append(node("p", "hypothesis-text", segment.hypothesis_text_at_export || "（没有识别文字）"));

  const form = node("form", "segment-form");
  form.dataset.segmentId = segment.segment_id;
  const textField = field("准确文字", "textarea", segment.reference_text || "", "先听原音，再逐字填写");
  textField.control.name = "reference_text";
  form.append(textField.wrapper);

  const grid = node("div", "field-grid");
  const speakerField = field("真实说话人", "input", segment.reference_speaker || "", "例如 self、mother、tv");
  speakerField.control.name = "reference_speaker";
  grid.append(speakerField.wrapper);

  const identityWrapper = node("label", "field");
  identityWrapper.append(node("span", "field-label", "是否本人"));
  const identity = node("select");
  identity.name = "reference_identity";
  [
    ["", "暂不判断"],
    ["self", "是本人"],
    ["not_self", "不是本人"],
  ].forEach(([value, label]) => {
    const option = node("option", "", label);
    option.value = value;
    option.selected = value === (segment.reference_identity || "");
    identity.append(option);
  });
  identityWrapper.append(identity);
  grid.append(identityWrapper);
  form.append(grid);

  const factsField = field(
    "关键事实",
    "input",
    (segment.key_facts || []).join("，"),
    "用逗号分隔，例如：明天十点，饭店",
  );
  factsField.control.name = "key_facts";
  form.append(factsField.wrapper);

  const notesField = field("备注", "input", segment.notes || "", "电视、远场、重叠或方言");
  notesField.control.name = "notes";
  form.append(notesField.wrapper);

  const footer = node("div", "form-footer");
  const includeLabel = node("label", "include-toggle");
  const include = node("input");
  include.type = "checkbox";
  include.name = "include";
  include.checked = segment.include !== false;
  includeLabel.append(include, node("span", "", "纳入评测"));
  footer.append(includeLabel);
  const save = node("button", "save-button", isTextLabeled(segment) ? "更新标注" : "保存标注");
  save.type = "submit";
  footer.append(save);
  form.append(footer);
  form.addEventListener("submit", (event) => saveSegment(event, segment, card));
  form.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  card.append(evidence, form);
  return card;
}

function field(label, kind, value, placeholder) {
  const wrapper = node("label", "field");
  wrapper.append(node("span", "field-label", label));
  const control = node(kind === "textarea" ? "textarea" : "input");
  if (kind !== "textarea") control.type = "text";
  control.value = value;
  control.placeholder = placeholder;
  wrapper.append(control);
  return { wrapper, control };
}

async function saveSegment(event, segment, card) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "保存中…";
  const facts = form.elements.key_facts.value
    .split(/[，,]/)
    .map((item) => item.trim())
    .filter(Boolean);
  const body = {
    reference_text: form.elements.reference_text.value,
    reference_speaker: form.elements.reference_speaker.value.trim(),
    reference_identity: form.elements.reference_identity.value,
    key_facts: facts,
    notes: form.elements.notes.value,
    include: form.elements.include.checked,
  };
  try {
    const payload = await api(
      `/api/evaluations/${state.recordingId}/${encodeURIComponent(state.evaluationName)}/segments/${segment.segment_id}`,
      { method: "PUT", body: JSON.stringify(body) },
    );
    Object.assign(segment, payload.segment);
    card.classList.toggle("saved", isTextLabeled(segment));
    button.textContent = "已保存";
    updateEvaluationProgress();
    updateTemplateProgress();
    toast(`片段 ${segment.segment_id} 已保存`);
  } catch (error) {
    button.textContent = "保存失败";
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    window.setTimeout(() => {
      button.textContent = isTextLabeled(segment) ? "更新标注" : "保存标注";
    }, 1200);
  }
}

function updateTemplateProgress() {
  const template = state.templates.find((item) => item.name === state.evaluationName);
  if (!template || !state.evaluation) return;
  template.text_labeled = state.evaluation.segments.filter(isTextLabeled).length;
  renderEvaluationOptions();
  updateEvaluationMetric();
}

function updateEvaluationProgress() {
  const segments = state.evaluation?.segments?.filter((item) => item.include !== false) || [];
  const labeled = segments.filter(isTextLabeled).length;
  $("#review-progress-label").textContent = `${labeled} / ${segments.length}`;
  $("#review-progress-bar").style.width = `${segments.length ? (labeled / segments.length) * 100 : 0}%`;
  updateEvaluationMetric();
}

function updateEvaluationMetric() {
  const template = state.templates.find((item) => item.name === state.evaluationName) || state.templates[0];
  if (!template) {
    $("#metric-evaluation").textContent = "0%";
    $("#metric-evaluation-detail").textContent = "没有评测模板";
    return;
  }
  const percent = template.segments ? Math.round((template.text_labeled / template.segments) * 100) : 0;
  $("#metric-evaluation").textContent = `${percent}%`;
  $("#metric-evaluation-detail").textContent = `${template.text_labeled} / ${template.segments} 已听写`;
}

function renderPagination(pages) {
  const container = $("#pagination");
  container.replaceChildren();
  if (pages <= 1) return;
  for (let page = 1; page <= pages; page += 1) {
    const button = node("button", `page-button ${page === state.page ? "active" : ""}`, page);
    button.addEventListener("click", () => {
      state.page = page;
      renderEvaluationSegments();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    container.append(button);
  }
}

async function runEvaluation() {
  if (!state.evaluationName) return;
  const button = $("#run-evaluation-button");
  button.disabled = true;
  button.textContent = "计算中…";
  try {
    const payload = await api(
      `/api/evaluations/${state.recordingId}/${encodeURIComponent(state.evaluationName)}/run`,
      { method: "POST", body: "{}" },
    );
    const metrics = payload.metrics;
    toast(
      `报告 #${payload.evaluation_run_id}：CER ${metric(metrics.text.cer)}，本人召回 ${metric(metrics.self_identity.recall)}`,
    );
    await loadRecordingWorkspace();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "生成评测报告";
  }
}

function renderActions() {
  const container = $("#action-list");
  const empty = $("#action-empty");
  container.replaceChildren();
  empty.classList.toggle("hidden", state.actions.length > 0);
  state.actions.forEach((action) => container.append(renderActionCard(action)));
}

function renderActionCard(action) {
  const card = node("article", "action-card");
  const header = node("header");
  header.append(node("span", "segment-id", `#${action.id} · ${action.type.toUpperCase()}`));
  header.append(node("span", `status-chip ${action.status}`, action.status));
  card.append(header);
  card.append(node("h4", "", action.title));
  const detail = node("div", "action-detail");
  detail.append(node("span", "", "时间"), node("strong", "", action.scheduled_at || action.time_text || "未解析"));
  detail.append(node("span", "", "地点"), node("strong", "", action.location || "未解析"));
  detail.append(node("span", "", "置信度"), node("strong", "", action.confidence.toFixed(2)));
  card.append(detail);
  const evidence = node("div", "evidence-box");
  const proposal = action.evidence?.proposal;
  const confirmation = action.evidence?.confirmation;
  evidence.append(node("strong", "", "提议："));
  evidence.append(document.createTextNode(proposal?.text || "无"));
  if (confirmation) {
    evidence.append(document.createElement("br"));
    evidence.append(node("strong", "", "确认："));
    evidence.append(document.createTextNode(confirmation.text || "无"));
  }
  card.append(evidence);
  const actions = node("div", "review-actions");
  const confirm = node("button", "review-button confirm", "确认");
  const dismiss = node("button", "review-button dismiss", "忽略");
  confirm.disabled = action.status === "confirmed";
  dismiss.disabled = action.status === "dismissed";
  confirm.addEventListener("click", () => reviewAction(action, "confirmed"));
  dismiss.addEventListener("click", () => reviewAction(action, "dismissed"));
  actions.append(confirm, dismiss);
  card.append(actions);
  return card;
}

async function reviewAction(action, status) {
  try {
    const updated = await api(`/api/actions/${action.id}/review`, {
      method: "POST",
      body: JSON.stringify({ status }),
    });
    Object.assign(action, updated);
    toast(`候选 #${action.id} 已更新为 ${status}`);
    await loadRecordingWorkspace();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderRuns() {
  const body = $("#run-table-body");
  body.replaceChildren();
  state.runs.forEach((run) => {
    const row = node("tr");
    row.append(node("td", "", `#${run.id}`));
    const statusCell = node("td");
    statusCell.append(node("span", `status-chip ${run.status === "completed" ? "confirmed" : "pending"}`, run.status));
    row.append(statusCell);
    row.append(node("td", "", formatDate(run.started_at)));
    row.append(node("td", "", run.config_sha256.slice(0, 12)));
    row.append(node("td", "", String(run.summary?.review_actions?.length || 0)));
    body.append(row);
  });
  if (!state.runs.length) {
    const row = node("tr");
    const cell = node("td", "", "还没有一键运行记录");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
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
        await loadRecordingWorkspace();
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

document.addEventListener("DOMContentLoaded", initialize);

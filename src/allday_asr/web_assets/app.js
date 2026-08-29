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
  timeline: null,
  semantic: null,
  timelineQueue: "conversation",
  timelineSelectedId: null,
  timelineWindow: null,
  filter: "all",
  page: 1,
  pageSize: 8,
  activeView: "timeline",
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
  $$(".timeline-queue-button").forEach((button) => {
    button.addEventListener("click", () => selectTimelineQueue(button.dataset.timelineQueue));
  });
}

function bindToolbar() {
  $("#recording-select").addEventListener("change", async (event) => {
    state.recordingId = Number(event.target.value);
    state.evaluationName = null;
    state.evaluation = null;
    state.timeline = null;
    state.semantic = null;
    state.timelineSelectedId = null;
    state.timelineWindow = null;
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
    const [dashboard, templates, actions, runs, timeline, semantic] = await Promise.all([
      api(`/api/dashboard?recording_id=${state.recordingId}`),
      api(`/api/evaluations?recording_id=${state.recordingId}`),
      api(`/api/actions?recording_id=${state.recordingId}`),
      api(`/api/runs?recording_id=${state.recordingId}`),
      api(`/api/speaker-timeline?recording_id=${state.recordingId}`),
      api(`/api/semantic?recording_id=${state.recordingId}`),
    ]);
    state.dashboard = dashboard;
    state.templates = templates.evaluations;
    state.actions = actions.actions;
    state.runs = runs.runs;
    state.timeline = timeline;
    state.semantic = semantic;
    renderDashboard();
    renderEvaluationOptions();
    renderActions();
    renderRuns();
    renderTimelineOverview();
    renderSemantic();
    await loadEvaluation();
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderTimelineOverview() {
  const timeline = state.timeline;
  const empty = $("#timeline-empty");
  const workspace = $("#timeline-workspace");
  if (!timeline?.available) {
    empty.classList.remove("hidden");
    workspace.classList.add("hidden");
    $("#timeline-empty-copy").textContent = timeline?.reason || "请先完成 V2-D 说话人运行。";
    $("#timeline-run-chip").textContent = "V2-D 不可用";
    return;
  }

  empty.classList.add("hidden");
  workspace.classList.remove("hidden");
  const revision = timeline.run.model_revision ? timeline.run.model_revision.slice(0, 8) : "local";
  $("#timeline-run-chip").textContent = `RUN #${timeline.run.id} · ${revision}`;
  $("#timeline-speaker-count").textContent = String(timeline.speakers.length);
  $("#timeline-speech-duration").textContent = formatDuration(timeline.speech_ms || 0);
  $("#timeline-overlap-duration").textContent = formatDuration(timeline.queues.overlap.total_ms || 0);
  $("#timeline-unassigned-count").textContent = String(timeline.queues.unassigned.token_count || 0);
  $("#timeline-conversation-count").textContent = String(timeline.queues.conversation.count);
  $("#timeline-overlap-count").textContent = String(timeline.queues.overlap.count);
  $("#timeline-unassigned-group-count").textContent = String(timeline.queues.unassigned.count);
  $("#timeline-possible-count").textContent = String(timeline.queues.possible?.count || 0);
  $("#timeline-identity-expansion-count").textContent = String(timeline.queues.identity_expansion?.count || 0);
  renderSpeakerLegend();
  renderTimelineV2D1Status();
  renderTimelineV2D2Status();
  renderTimelineV2D3Status();
  selectTimelineQueue(state.timelineQueue, { preserveSelection: true });
}

function renderSpeakerLegend() {
  const container = $("#speaker-legend");
  container.replaceChildren();
  state.timeline.speakers.forEach((speaker) => {
    const item = node("div", "speaker-legend-item");
    const swatch = node("span", "speaker-swatch");
    swatch.style.backgroundColor = speaker.color;
    item.append(swatch, node("strong", "", speaker.label));
    item.append(node("small", "", `${formatDuration(speaker.speech_ms)} · ${speaker.turn_count} 段`));
    if (speaker.identity_audit) {
      const identities = speaker.identity_audit.identities
        .map((value) => `${identityLabel(value.identity)} ${formatDuration(value.overlap_ms)}`)
        .join(" / ");
      item.classList.toggle("contaminated", speaker.identity_audit.contaminated);
      item.append(node(
        "small",
        "speaker-identity-audit",
        `${speaker.identity_audit.contaminated ? "污染簇 · " : "人工重叠 · "}${identities}`,
      ));
    }
    container.append(item);
  });
  const note = node("p", "speaker-legend-note", "标签是匿名聚类，不代表真实身份；重叠轨使用原始多轨结果。");
  container.append(note);
}

function renderTimelineV2D1Status() {
  const container = $("#timeline-v2d1-status");
  const refinement = state.timeline.v2d1;
  container.replaceChildren();
  if (!refinement?.available) {
    container.classList.add("unavailable");
    container.append(
      node("strong", "", "V2-D.1 尚未生成"),
      node("span", "", refinement?.reason || "当前只显示正式说话人结果。"),
    );
    return;
  }
  container.classList.remove("unavailable");
  container.append(
    node("strong", "", `V2-D.1 · RUN #${refinement.run_id}`),
    node("span", "", `确定语音 ${formatDuration(refinement.detected_ms)} · 可能语音 ${formatDuration(refinement.possible_ms)}`),
    node("span", "timeline-v2d1-policy", "来源只是一条独立真值轨；电视中的不同男女声仍保留为不同匿名 speaker。"),
  );
}

function renderTimelineV2D2Status() {
  const container = $("#timeline-v2d2-status");
  const auditPanel = $("#timeline-identity-audit");
  const audit = state.timeline.v2d2;
  container.replaceChildren();
  auditPanel.replaceChildren();
  if (!audit?.available) {
    container.classList.add("unavailable");
    container.append(
      node("strong", "", "V2-D.2 尚未生成"),
      node("span", "", audit?.reason || "当前没有人工身份污染审计。"),
    );
    auditPanel.classList.add("hidden");
    return;
  }
  container.classList.remove("unavailable");
  container.append(
    node("strong", "", `V2-D.2 · RUN #${audit.run_id}`),
    node("span", "", `人工身份 ${formatDuration(audit.reviewed_truth_ms)} · 模型覆盖 ${(audit.coverage * 100).toFixed(1)}%`),
    node(
      "span",
      `timeline-v2d2-warning ${audit.contaminated_speakers.length ? "active" : ""}`,
      audit.contaminated_speakers.length
        ? `污染簇：${audit.contaminated_speakers.join("、")}`
        : "当前已审计范围未发现污染簇",
    ),
    node("span", "timeline-v2d1-policy", "只标注真值覆盖的短区间，不把整个匿名簇改名。"),
  );
  auditPanel.classList.remove("hidden");
  const header = node("header", "identity-audit-heading");
  header.append(
    node("strong", "", "匿名 speaker 污染矩阵"),
    node("span", "", `冻结真值 #${audit.truth_set_id} · ${audit.truth_name}`),
  );
  auditPanel.append(header);
  const table = node("table", "identity-audit-table");
  const thead = node("thead");
  const headerRow = node("tr");
  ["匿名 speaker", "人工身份重叠", "主身份纯度", "结论"].forEach((label) => {
    headerRow.append(node("th", "", label));
  });
  thead.append(headerRow);
  table.append(thead);
  const body = node("tbody");
  audit.model_speakers.forEach((speaker) => {
    const row = node("tr", speaker.contaminated ? "contaminated" : "");
    const label = node("td", "identity-model-speaker");
    const swatch = node("span", "speaker-swatch");
    swatch.style.backgroundColor = speakerColor(speaker.speaker);
    label.append(swatch, node("strong", "", speaker.speaker));
    row.append(label);
    const identities = node("td", "identity-audit-mix");
    speaker.identities.forEach((value) => {
      const chip = node("span", "identity-chip", `${identityLabel(value.identity)} ${formatDuration(value.overlap_ms)}`);
      chip.style.setProperty("--identity-color", identityColor(value.identity));
      identities.append(chip);
    });
    row.append(identities);
    row.append(node("td", "", `${(speaker.purity * 100).toFixed(1)}%`));
    row.append(node("td", speaker.contaminated ? "audit-danger" : "audit-ok", speaker.contaminated ? "人物混入同簇" : "审计范围内单一"));
    body.append(row);
  });
  table.append(body);
  auditPanel.append(table);
}

function renderTimelineV2D3Status() {
  const container = $("#timeline-v2d3-status");
  const expansion = state.timeline.v2d3;
  container.replaceChildren();
  if (!expansion?.available) {
    container.classList.add("unavailable");
    container.append(
      node("strong", "", "V2-D.3 尚未生成"),
      node("span", "", expansion?.reason || "当前没有身份扩样候选。"),
    );
    return;
  }
  container.classList.remove("unavailable");
  const negatives = expansion.negative_identities.map(identityLabel).join(" / ") || "无";
  container.append(
    node("strong", "", `V2-D.3 · RUN #${expansion.run_id}`),
    node(
      "span",
      "",
      `${identityLabel(expansion.target_identity)}种子 ${formatDuration(expansion.target_truth_ms)}`
      + ` · ${expansion.target_embedding_count} 个 embedding · ${expansion.selected_candidates} 个候选`
      + ` · 已审 ${expansion.reviewed_candidates || 0}`,
    ),
    node("span", "timeline-v2d3-negatives", `负对照：${negatives}`),
    node(
      "span",
      expansion.enrollment_ready ? "audit-ok" : "timeline-v2d2-warning active",
      expansion.enrollment_ready
        ? "种子已达到登记时长门槛；候选仍需人工确认"
        : "弱种子：未达到正式声纹登记门槛，所有分数只用于排序",
    ),
  );
}

function selectTimelineQueue(queueName, options = {}) {
  if (!state.timeline?.available || !state.timeline.queues[queueName]) return;
  state.timelineQueue = queueName;
  const queue = state.timeline.queues[queueName];
  $$(".timeline-queue-button").forEach((button) => {
    const active = button.dataset.timelineQueue === queueName;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("#timeline-queue-title").textContent = queue.label;
  $("#timeline-queue-description").textContent = queue.description;
  const selectionExists = queue.items.some((item) => item.id === state.timelineSelectedId);
  if (!options.preserveSelection || !selectionExists) {
    state.timelineSelectedId = queue.items[0]?.id || null;
    state.timelineWindow = null;
  }
  renderTimelineCandidates();
  if (state.timelineSelectedId) {
    loadTimelineCandidate(state.timelineSelectedId);
  } else {
    renderTimelineDetailEmpty("这个队列没有候选片段。", "换一个队列继续查看。");
  }
}

function renderTimelineCandidates() {
  const queue = state.timeline.queues[state.timelineQueue];
  const container = $("#timeline-candidate-list");
  container.replaceChildren();
  queue.items.forEach((item, index) => {
    const button = node(
      "button",
      `timeline-candidate ${item.id === state.timelineSelectedId ? "active" : ""}`,
    );
    button.type = "button";
    button.dataset.candidateId = item.id;
    const header = node("span", "timeline-candidate-title");
    header.append(node("strong", "", item.title));
    header.append(node("span", "", `${formatOffset(item.start_ms)}–${formatOffset(item.end_ms)}`));
    button.append(header);
    const facts = node("span", "timeline-candidate-facts");
    if (item.speaker_count) facts.append(node("span", "", `${item.speaker_count} 人`));
    if (item.speaker_switches !== null) facts.append(node("span", "", `${item.speaker_switches} 次切换`));
    if (item.overlap_ms) facts.append(node("span", "", `重叠 ${(item.overlap_ms / 1000).toFixed(1)}s`));
    if (item.possible_ms) facts.append(node("span", "possible-fact", `可能 ${(item.possible_ms / 1000).toFixed(1)}s`));
    if (item.has_rejected_asr) facts.append(node("span", "possible-fact", "含拒绝 ASR 证据"));
    if (item.identity_labels?.length) {
      facts.append(node("span", "identity-fact", `人工：${item.identity_labels.map(identityLabel).join(" / ")}`));
    }
    if (item.kind === "identity_expansion") {
      facts.append(node("span", "identity-fact", `目标 ${Number(item.target_similarity).toFixed(3)}`));
      facts.append(node("span", item.contrastive_margin >= 0 ? "identity-fact" : "possible-fact", `对照差 ${Number(item.contrastive_margin).toFixed(3)}`));
      if (item.review_status) facts.append(node("span", `identity-review-badge ${item.review_status}`, identityReviewLabel(item.review_status)));
    }
    facts.append(node("span", "", `${item.token_count} 词`));
    button.append(facts);
    if (item.preview) button.append(node("span", "timeline-candidate-preview", item.preview));
    button.addEventListener("click", () => loadTimelineCandidate(item.id));
    container.append(button);
    if (item.id === state.timelineSelectedId) {
      $("#timeline-queue-position").textContent = `${index + 1} / ${queue.items.length}`;
    }
  });
  if (!queue.items.length) $("#timeline-queue-position").textContent = "0 / 0";
}

async function loadTimelineCandidate(candidateId) {
  const queue = state.timeline.queues[state.timelineQueue];
  const item = queue.items.find((candidate) => candidate.id === candidateId);
  if (!item) return;
  state.timelineSelectedId = candidateId;
  renderTimelineCandidates();
  const detail = $("#timeline-detail");
  detail.replaceChildren(node("div", "timeline-loading", "正在读取时间轴…"));
  try {
    const payload = await api(
      `/api/speaker-timeline/window?recording_id=${state.recordingId}`
      + `&run_id=${state.timeline.run.id}&start_ms=${item.start_ms}&end_ms=${item.end_ms}`,
    );
    if (state.timelineSelectedId !== candidateId) return;
    state.timelineWindow = payload;
    renderTimelineDetail(item, payload);
  } catch (error) {
    renderTimelineDetailEmpty("时间轴读取失败", error.message);
    toast(error.message, "error");
  }
}

function renderTimelineDetail(item, payload) {
  const detail = $("#timeline-detail");
  detail.replaceChildren();
  const header = node("header", "timeline-detail-header");
  const heading = node("div");
  heading.append(node("span", "segment-id", item.kind.toUpperCase()));
  heading.append(node("h4", "", item.title));
  heading.append(node("p", "", `${formatOffset(item.start_ms)}–${formatOffset(item.end_ms)} · ${formatDuration(item.end_ms - item.start_ms)}`));
  header.append(heading);
  const navigation = node("div", "timeline-detail-navigation");
  const queue = state.timeline.queues[state.timelineQueue];
  const index = queue.items.findIndex((candidate) => candidate.id === item.id);
  const previous = node("button", "secondary-button", "上一个");
  const next = node("button", "primary-button", "下一个");
  previous.disabled = index <= 0;
  next.disabled = index < 0 || index >= queue.items.length - 1;
  previous.addEventListener("click", () => loadTimelineCandidate(queue.items[index - 1].id));
  next.addEventListener("click", () => loadTimelineCandidate(queue.items[index + 1].id));
  navigation.append(previous, next);
  header.append(navigation);
  detail.append(header);

  const facts = node("div", "timeline-detail-facts");
  if (item.score !== undefined) facts.append(timelineFact("信息分", item.score));
  if (item.speech_ms !== null) facts.append(timelineFact("有效语音", formatDuration(item.speech_ms)));
  if (item.possible_ms) facts.append(timelineFact("低置信语音", formatDuration(item.possible_ms)));
  if (item.identity_truth_ms) facts.append(timelineFact("人工身份真值", formatDuration(item.identity_truth_ms)));
  if (item.kind === "identity_expansion") {
    facts.append(timelineFact(`${identityLabel(item.target_identity)}相似度`, Number(item.target_similarity).toFixed(3)));
    facts.append(timelineFact(`最强负对照 · ${identityLabel(item.negative_identity)}`, Number(item.negative_similarity).toFixed(3)));
    facts.append(timelineFact("对照差值", Number(item.contrastive_margin).toFixed(3)));
  }
  if (item.speaker_switches !== null) facts.append(timelineFact("说话人切换", item.speaker_switches));
  facts.append(timelineFact("重叠", `${(item.overlap_ms / 1000).toFixed(1)}s`));
  facts.append(timelineFact("无归属", item.unassigned_tokens));
  detail.append(facts);

  if (item.kind === "identity_expansion") {
    const notice = node("div", `identity-expansion-notice ${item.review_tier || "exploratory"}`);
    notice.append(
      node("strong", "", "弱种子候选，不是身份结论"),
      node(
        "p",
        "",
        `该段只是比最强负对照“${identityLabel(item.negative_identity)}”高 ${Number(item.contrastive_margin).toFixed(3)}。`
        + "分数未经身份阈值校准；试听后也不会自动写入人物或正式声纹。",
      ),
    );
    detail.append(notice);
    detail.append(renderIdentityReviewControls(item, payload));
  }

  const audioBlock = node("div", "timeline-audio-block");
  audioBlock.append(node("strong", "", "响度增强试听（派生缓存）"));
  audioBlock.append(node("p", "", "只从永久原音读取这一小段；不会修改或替换原始文件。"));
  const audio = node("audio");
  audio.controls = true;
  audio.preload = "metadata";
  audio.src = payload.audio_url;
  audio.setAttribute("aria-label", `播放 ${item.title}`);
  audioBlock.append(audio);
  detail.append(audioBlock);

  detail.append(renderSpeakerTracks(payload));
  detail.append(renderTimelineTranscript(payload, audio));
}

function renderIdentityReviewControls(item, payload) {
  const wrapper = node("section", "identity-review-controls");
  const heading = node("div", "identity-review-heading");
  heading.append(
    node("strong", "", `试听后判断是不是${identityLabel(item.target_identity)}`),
    node("span", "", item.review_status ? `当前：${identityReviewLabel(item.review_status)}` : "尚未判断"),
  );
  wrapper.append(heading);
  const actions = node("div", "identity-review-actions");
  [
    ["confirmed_target", `是${identityLabel(item.target_identity)}`],
    ["rejected", "不是"],
    ["uncertain", "听不清"],
  ].forEach(([status, label]) => {
    const button = node("button", `identity-review-button ${status} ${item.review_status === status ? "active" : ""}`, label);
    button.type = "button";
    button.addEventListener("click", () => submitIdentityReview(item, status, payload, actions));
    actions.append(button);
  });
  wrapper.append(actions);
  wrapper.append(node("p", "", "这里只保存人工审核覆盖层；即使选择“是”，也不会自动登记人物或写入正式声纹。"));
  return wrapper;
}

async function submitIdentityReview(item, status, payload, actions) {
  actions.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const review = await api("/api/speaker-timeline/identity-review", {
      method: "POST",
      body: JSON.stringify({
        recording_id: state.recordingId,
        run_id: state.timeline.v2d3.run_id,
        candidate_id: item.id,
        status,
      }),
    });
    item.review_status = review.status;
    item.reviewed_at = review.updated_at;
    const reviewed = state.timeline.queues.identity_expansion.items.filter((candidate) => candidate.review_status);
    state.timeline.v2d3.reviewed_candidates = reviewed.length;
    state.timeline.v2d3.review_counts = {
      confirmed_target: reviewed.filter((candidate) => candidate.review_status === "confirmed_target").length,
      rejected: reviewed.filter((candidate) => candidate.review_status === "rejected").length,
      uncertain: reviewed.filter((candidate) => candidate.review_status === "uncertain").length,
    };
    state.timeline.v2d3.confirmed_ms = reviewed
      .filter((candidate) => candidate.review_status === "confirmed_target")
      .reduce((total, candidate) => total + candidate.end_ms - candidate.start_ms, 0);
    renderTimelineCandidates();
    renderTimelineV2D3Status();
    renderTimelineDetail(item, payload);
    toast(`${item.title}：${identityReviewLabel(status)}`);
  } catch (error) {
    actions.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    toast(error.message, "error");
  }
}

function identityReviewLabel(status) {
  return {
    confirmed_target: "确认是目标人物",
    rejected: "确认不是",
    uncertain: "听不清",
  }[status] || "尚未判断";
}

function timelineFact(label, value) {
  const item = node("div", "timeline-fact");
  item.append(node("span", "", label), node("strong", "", value));
  return item;
}

function renderSpeakerTracks(payload) {
  const wrapper = node("section", "speaker-tracks");
  const title = node("div", "timeline-subheading");
  title.append(node("strong", "", "说话人轨道"));
  title.append(node("span", "", "琥珀色 = 可能语音；斜纹 = 同时说话"));
  wrapper.append(title);
  const span = payload.end_ms - payload.start_ms;
  if (payload.speech_evidence?.length) {
    const row = evidenceTrackRow("语音证据", "evidence");
    payload.speech_evidence.forEach((evidence) => {
      const bar = positionedTrackBar(
        evidence,
        payload,
        `speech-evidence ${evidence.tier === "possible" ? "possible" : "detected"}`,
      );
      const evidenceTypes = evidence.evidence_types?.join(" + ") || "上下文桥接";
      bar.title = `${evidence.tier === "possible" ? "可能语音" : "确定语音"} · ${formatOffset(evidence.source_start_ms)}–${formatOffset(evidence.source_end_ms)} · ${evidenceTypes}`;
      row.track.append(bar);
    });
    wrapper.append(row.container);
  }
  if (payload.identity_regions?.length) {
    const row = evidenceTrackRow("人工身份", "identity");
    payload.identity_regions.forEach((region) => {
      const bar = positionedTrackBar(region, payload, "manual-identity-evidence");
      bar.style.backgroundColor = identityColor(region.identity);
      bar.title = `${identityLabel(region.identity)} · ${formatOffset(region.source_start_ms)}–${formatOffset(region.source_end_ms)} · 人工真值，只审计短区间`;
      row.track.append(bar);
    });
    wrapper.append(row.container);
  }
  if (payload.source_regions?.length) {
    const row = evidenceTrackRow("来源真值", "source");
    payload.source_regions.forEach((region) => {
      const bar = positionedTrackBar(region, payload, `source-evidence ${region.source}`);
      bar.title = `${speechSourceLabel(region.source)} · 真值集 #${region.truth_set_id} · 不合并 speaker`;
      row.track.append(bar);
    });
    wrapper.append(row.container);
  }
  const labels = [...new Set(payload.turns.map((turn) => turn.speaker))];
  labels.sort((a, b) => speakerIndex(a) - speakerIndex(b));
  labels.forEach((label) => {
    const row = node("div", "speaker-track-row");
    const legend = node("div", "speaker-track-label");
    const swatch = node("span", "speaker-swatch");
    swatch.style.backgroundColor = speakerColor(label);
    legend.append(swatch, node("span", "", label));
    row.append(legend);
    const track = node("div", "speaker-track");
    payload.turns.filter((turn) => turn.speaker === label).forEach((turn) => {
      const bar = node("span", "speaker-turn");
      bar.style.left = `${((turn.start_ms - payload.start_ms) / span) * 100}%`;
      bar.style.width = `${Math.max(0.2, ((turn.end_ms - turn.start_ms) / span) * 100)}%`;
      bar.style.backgroundColor = speakerColor(label);
      const truth = turn.identity_evidence?.evidence
        ?.map((value) => `${identityLabel(value.identity)} ${(value.overlap_ratio * 100).toFixed(0)}%`)
        .join(" / ");
      bar.title = `${label} · ${formatOffset(turn.source_start_ms)}–${formatOffset(turn.source_end_ms)}${truth ? ` · 人工重叠 ${truth}` : ""}`;
      track.append(bar);
    });
    payload.overlaps.forEach((overlap) => {
      if (!overlap.speakers.includes(label)) return;
      const stripe = node("span", "speaker-overlap");
      stripe.style.left = `${((overlap.start_ms - payload.start_ms) / span) * 100}%`;
      stripe.style.width = `${Math.max(0.25, ((overlap.end_ms - overlap.start_ms) / span) * 100)}%`;
      track.append(stripe);
    });
    row.append(track);
    wrapper.append(row);
  });
  const ruler = node("div", "timeline-ruler");
  ruler.append(node("span", "", formatOffset(payload.start_ms)));
  ruler.append(node("span", "", formatOffset(payload.start_ms + Math.round(span / 2))));
  ruler.append(node("span", "", formatOffset(payload.end_ms)));
  wrapper.append(ruler);
  return wrapper;
}

function evidenceTrackRow(label, kind) {
  const container = node("div", `speaker-track-row evidence-track-row ${kind}`);
  const legend = node("div", "speaker-track-label");
  const swatch = node("span", `evidence-swatch ${kind}`);
  legend.append(swatch, node("span", "", label));
  const track = node("div", "speaker-track evidence-track");
  container.append(legend, track);
  return { container, track };
}

function positionedTrackBar(region, payload, className) {
  const span = payload.end_ms - payload.start_ms;
  const bar = node("span", className);
  bar.style.left = `${((region.start_ms - payload.start_ms) / span) * 100}%`;
  bar.style.width = `${Math.max(0.2, ((region.end_ms - region.start_ms) / span) * 100)}%`;
  return bar;
}

function speechSourceLabel(source) {
  return {
    media_playback: "媒体播放",
    live_person: "现场人物",
    mixed_live_media: "现场 + 媒体混合",
    unknown: "来源未知",
  }[source] || source;
}

function identityLabel(identity) {
  return {
    mother: "母亲",
    father: "父亲",
    tv: "电视",
    self: "本人",
    me: "本人",
  }[identity] || identity;
}

function identityColor(identity) {
  return {
    mother: "#c95f56",
    father: "#5f7fa9",
    tv: "#8172c6",
    self: "#3f8f91",
    me: "#3f8f91",
  }[identity] || "#7d8589";
}

function renderTimelineTranscript(payload, audio) {
  const wrapper = node("section", "timeline-transcript");
  const title = node("div", "timeline-subheading");
  title.append(node("strong", "", "对齐文字"));
  title.append(node("span", "", "点击任一词跳到对应时间；红色虚线 = 无归属"));
  wrapper.append(title);
  const tokens = node("div", "timeline-token-list");
  payload.tokens.forEach((token) => {
    const button = node("button", "timeline-token", token.text || "·");
    button.type = "button";
    const label = token.primary_speaker;
    if (token.primary_kind === "none") {
      button.classList.add("unassigned");
    } else if (label) {
      button.style.setProperty("--token-color", speakerColor(label));
    }
    if (token.attributions.some((item) => item.kind === "overlap")) {
      button.classList.add("overlap");
    }
    const identity = token.identity_evidence?.primary_identity;
    if (identity) {
      const truthBadge = node("span", "timeline-token-identity", identityLabel(identity));
      truthBadge.style.setProperty("--identity-color", identityColor(identity));
      button.append(truthBadge);
      button.classList.add("has-identity-truth");
    }
    const decisions = token.attributions
      .map((item) => `${item.kind}: ${item.speaker || "无"}`)
      .join(" / ");
    const identityEvidence = token.identity_evidence?.evidence
      ?.map((value) => `${identityLabel(value.identity)} ${(value.overlap_ratio * 100).toFixed(0)}%`)
      .join(" / ");
    button.title = `${formatOffset(token.start_ms)} · ${decisions || "无归属"}${identityEvidence ? ` · 人工真值 ${identityEvidence}` : ""}`;
    button.addEventListener("click", () => {
      audio.currentTime = Math.max(0, (token.start_ms - payload.start_ms) / 1000);
      audio.play().catch(() => {});
    });
    tokens.append(button);
  });
  if (!payload.tokens.length) tokens.append(node("p", "timeline-no-tokens", "这个窗口没有 ASR 对齐文字。"));
  wrapper.append(tokens);
  return wrapper;
}

function speakerIndex(label) {
  return state.timeline.speakers.findIndex((speaker) => speaker.label === label);
}

function speakerColor(label) {
  return state.timeline.speakers.find((speaker) => speaker.label === label)?.color || "#65717d";
}

function renderTimelineDetailEmpty(title, copy) {
  const detail = $("#timeline-detail");
  detail.replaceChildren();
  const empty = node("div", "timeline-detail-empty");
  empty.append(node("strong", "", title), node("p", "", copy));
  detail.append(empty);
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

  evidence.append(
    renderAudioPlayer(
      "评测片段 · 准确文字只抄这里",
      segment.audio_url,
      `播放严格片段 ${segment.segment_id}`,
      "exact",
    ),
  );
  evidence.append(
    renderAudioPlayer(
      "辅助上下文 · 前后各 3 秒，不抄片段外文字",
      segment.context_audio_url,
      `播放片段 ${segment.segment_id} 的上下文`,
      "context",
    ),
  );
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
  speakerField.control.setAttribute("list", "speaker-labels");
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

function renderAudioPlayer(label, source, ariaLabel, variant) {
  const wrapper = node("div", `audio-player ${variant}`);
  wrapper.append(node("small", "audio-note", label));
  const audio = node("audio");
  audio.controls = true;
  audio.preload = "none";
  audio.src = source;
  audio.setAttribute("aria-label", ariaLabel);
  wrapper.append(audio);
  return wrapper;
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

function renderSemantic() {
  const payload = state.semantic;
  const empty = $("#semantic-empty");
  const workspace = $("#semantic-workspace");
  const generate = $("#generate-semantic-button");
  if (!payload?.available) {
    empty.classList.remove("hidden");
    workspace.classList.add("hidden");
    $("#semantic-empty-copy").textContent = payload?.reason || "完成 V2-C/V2-D 后即可生成。";
    generate.disabled = payload?.can_generate === false;
    updateSemanticBadge(0);
    return;
  }

  empty.classList.add("hidden");
  workspace.classList.remove("hidden");
  generate.disabled = false;
  const summary = payload.summary || {};
  const conversationCount = summary.conversation_count ?? summary.event_count ?? 0;
  const excludedCount = summary.excluded_block_count ?? 0;
  const jobCount = summary.llm_job_count ?? 0;
  $("#semantic-conversation-count").textContent = String(conversationCount);
  $("#semantic-excluded-count").textContent = String(excludedCount);
  $("#semantic-job-count").textContent = jobCount ? String(jobCount) : "旧版";
  $("#semantic-token-count").textContent = Number(summary.token_count || 0).toLocaleString("zh-CN");
  $("#semantic-reviewed-count").textContent = `${payload.reviewed_candidates} / ${payload.candidates.length}`;
  $("#semantic-provider").textContent = payload.run.provider === "local_mock" ? "本地 mock" : payload.run.provider;
  $("#semantic-run-note").textContent = (
    `RUN #${payload.run.id} · ASR #${payload.run.asr_run_id}`
    + `${payload.run.diarization_run_id ? ` · DIARIZATION #${payload.run.diarization_run_id}` : ""}`
    + ` · REQUEST ${payload.run.request_sha256.slice(0, 12)}`
  );
  const transport = payload.transport || {};
  $("#semantic-transport-note").textContent = payload.version === "v2-e.0.1"
    ? `${conversationCount} 个完整对话 → ${jobCount} 个计划请求；对话无时长硬切，${Math.round((transport.review_clip_ms || 120000) / 1000)} 秒仅是播放器切片。`
    : "这是旧版 120 秒证据分组；重新生成后会升级为完整对话传输计划。";
  renderSemanticExcluded(payload.excluded_blocks || []);
  const pending = payload.candidates.filter((item) => !item.review_status).length;
  updateSemanticBadge(pending);
  const container = $("#semantic-candidate-list");
  container.replaceChildren();
  payload.candidates.forEach((candidate) => container.append(renderSemanticCard(candidate)));
}

function renderSemanticExcluded(items) {
  const panel = $("#semantic-excluded-panel");
  const container = $("#semantic-excluded-list");
  container.replaceChildren();
  panel.classList.toggle("hidden", items.length === 0);
  items.forEach((item) => {
    const row = node("div", "semantic-excluded-item");
    row.append(
      node("strong", "", `${formatOffset(item.start_ms)}–${formatOffset(item.end_ms)}`),
      node("span", "", item.transcript || "（空）"),
      node("small", "", `${item.token_count} tokens · 信息字符 ${item.informative_char_count}`),
    );
    container.append(row);
  });
}

function updateSemanticBadge(count) {
  const badge = $("#semantic-review-badge");
  badge.textContent = String(count);
  badge.classList.toggle("hidden", count === 0);
}

function renderSemanticCard(candidate) {
  const card = node("article", `semantic-card ${candidate.type} ${candidate.review_status || "pending"}`);
  const header = node("header", "semantic-card-header");
  const meta = node("div", "semantic-card-meta");
  const isConversation = candidate.semantic_unit === "conversation";
  meta.append(node("span", "segment-id", candidate.type === "daily_summary" ? "DAY EVIDENCE" : isConversation ? "FULL CONVERSATION" : "EVENT EVIDENCE"));
  meta.append(node("span", "time-chip", `${formatOffset(candidate.start_ms)}–${formatOffset(candidate.end_ms)}`));
  const status = node(
    "span",
    `status-chip ${candidate.review_status || "pending"}`,
    candidate.review_status === "confirmed" ? "已确认" : candidate.review_status === "rejected" ? "已排除" : "待审核",
  );
  header.append(meta, status);
  card.append(header);

  const title = node("input", "semantic-title-input");
  title.type = "text";
  title.value = candidate.title;
  title.setAttribute("aria-label", "语义候选标题");
  const body = node("textarea", "semantic-body-input");
  body.value = candidate.body;
  body.rows = candidate.type === "daily_summary" ? 3 : 4;
  body.setAttribute("aria-label", "语义候选内容");
  card.append(title, body);

  if (isConversation && candidate.review_clips?.length) {
    card.append(renderConversationReviewPlayer(candidate));
  } else if (candidate.audio_url) {
    card.append(renderAudioPlayer("本地原音证据", candidate.audio_url, `播放语义证据 ${candidate.id}`, "exact"));
  }
  const evidence = node("div", "semantic-evidence-row");
  if (candidate.type === "daily_summary") {
    evidence.append(node("span", "semantic-evidence-chip", `${candidate.evidence.conversation_keys?.length || candidate.evidence.event_keys?.length || 0} 个对话引用`));
  } else {
    const uncertainty = candidate.evidence.uncertainty || {};
    evidence.append(
      node("span", "semantic-evidence-chip", `${candidate.evidence.token_ids?.length || 0} tokens`),
      node("span", "semantic-evidence-chip", `${candidate.evidence.utterance_keys?.length || 0} 说话轮次`),
      node("span", "semantic-evidence-chip", `${candidate.review_clips?.length || 1} 个回听切片`),
      node("span", "semantic-evidence-chip warning", `${uncertainty.unassigned_tokens || 0} 无归属`),
      node("span", "semantic-evidence-chip warning", `${candidate.evidence.asr_alternative_windows || uncertainty.disagreement_ids?.length || 0} 模型分歧`),
    );
  }
  card.append(evidence);

  const note = node("input", "semantic-note-input");
  note.type = "text";
  note.value = candidate.review_note || "";
  note.placeholder = "可选审核备注";
  note.setAttribute("aria-label", "语义审核备注");
  const actions = node("div", "semantic-review-actions");
  const confirm = node("button", "review-button confirm", "确认并保存修订");
  const reject = node("button", "review-button dismiss", "排除");
  confirm.type = "button";
  reject.type = "button";
  confirm.addEventListener("click", () => reviewSemantic(candidate, "confirmed", title, body, note, confirm));
  reject.addEventListener("click", () => reviewSemantic(candidate, "rejected", title, body, note, reject));
  actions.append(confirm, reject);
  card.append(note, actions);
  return card;
}

function renderConversationReviewPlayer(candidate) {
  const wrapper = node("div", "semantic-review-player");
  const label = node("label", "semantic-clip-select");
  label.append(node("span", "", "本地回听切片（不会切断发送给 LLM 的对话）"));
  const select = node("select");
  candidate.review_clips.forEach((clip) => {
    const option = node("option", "", `片段 ${clip.index} · ${formatOffset(clip.start_ms)}–${formatOffset(clip.end_ms)}`);
    option.value = clip.audio_url;
    select.append(option);
  });
  label.append(select);
  const player = renderAudioPlayer("当前切片", candidate.review_clips[0].audio_url, `播放完整对话 ${candidate.id} 的回听切片`, "exact");
  const audio = player.querySelector("audio");
  select.addEventListener("change", () => {
    audio.src = select.value;
    audio.load();
  });
  wrapper.append(label, player);
  return wrapper;
}

async function generateSemantic() {
  if (!state.recordingId) return;
  const button = $("#generate-semantic-button");
  button.disabled = true;
  button.textContent = "整理证据中…";
  try {
    const result = await api("/api/semantic/generate", {
      method: "POST",
      body: JSON.stringify({ recording_id: state.recordingId }),
    });
    toast(`V2-E.0.1 run #${result.run_id}：${result.conversation_count} 个完整对话，${result.excluded_block_count} 个低信息块不发送`);
    state.semantic = await api(`/api/semantic?recording_id=${state.recordingId}`);
    renderSemantic();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "重新生成本地证据包";
  }
}

async function reviewSemantic(candidate, status, title, body, note, button) {
  button.disabled = true;
  try {
    await api(`/api/semantic/candidates/${candidate.id}/review`, {
      method: "POST",
      body: JSON.stringify({
        recording_id: state.recordingId,
        status,
        title: title.value,
        body: body.value,
        note: note.value,
      }),
    });
    state.semantic = await api(`/api/semantic?recording_id=${state.recordingId}`);
    renderSemantic();
    toast(`语义证据 #${candidate.id} 已${status === "confirmed" ? "确认" : "排除"}`);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
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

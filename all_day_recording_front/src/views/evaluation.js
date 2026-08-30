import { api } from '../api/client.js'
import { sessionBody, sessionQuery, state } from '../state/workspace.js'
import { formatDuration, formatOffset, metric } from '../utils/format.js'
import { $, node, toast } from '../workspace/dom.js'
import { reloadWorkspace } from '../workspace/reload.js'
import { timelineFact } from './timeline.js'

export function renderEvaluationOptions() {
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

export function renderSessionEvaluation() {
  const container = $("#session-evaluation-panel");
  const payload = state.sessionEvaluation;
  const legacyOnly = Boolean(state.recordingId);
  $("#view-evaluation .section-actions").classList.toggle("hidden", !legacyOnly);
  $("#view-evaluation .review-toolbar").classList.toggle("hidden", !legacyOnly);
  $("#view-evaluation .annotation-purpose").classList.toggle("hidden", !legacyOnly);
  container.replaceChildren();
  if (!payload || legacyOnly) {
    container.classList.add("hidden");
    return;
  }
  container.classList.remove("hidden");
  const heading = node("header", "session-evaluation-heading");
  const title = node("div");
  title.append(
    node("strong", "", "V2-D.1 人工区间评测"),
    node("p", "", "只评测你已经逐条听过的琥珀区间；听不清的区间自动排除。"),
  );
  heading.append(title);
  if (payload.can_create) {
    const button = node(
      "button",
      "primary-button",
      payload.available ? "重新读取评测结果" : "生成连续评测真值",
    );
    button.type = "button";
    button.addEventListener("click", () => createSessionEvaluation(button));
    heading.append(button);
  }
  container.append(heading);

  const review = payload.review || {};
  const facts = node("div", "session-evaluation-facts");
  facts.append(
    timelineFact("D.1 已审核", `${review.reviewed_count || 0}/${review.candidate_count || 0}`),
    timelineFact("确认语音", review.status_counts?.confirmed_speech || 0),
    timelineFact("确认非语音", review.status_counts?.rejected || 0),
    timelineFact("听不清排除", review.status_counts?.uncertain || 0),
  );
  container.append(facts);

  if (!payload.truth) {
    container.append(node("p", "session-evaluation-note", payload.reason || "尚未生成连续真值。"));
    return;
  }
  const truth = payload.truth;
  container.append(node(
    "p",
    "session-evaluation-note",
    `冻结真值 #${truth.truth_set_id} · ${truth.evaluation_region_count} 个离散审核范围`
      + ` · 覆盖 ${formatDuration(truth.evaluated_duration_ms)} · 人声 ${formatDuration(truth.speech_duration_ms)}`,
  ));
  const results = node("div", "session-evaluation-results");
  [
    ["detected", "原始确定层"],
    ["recall_rescue", "加入召回补救层"],
  ].forEach(([key, label]) => {
    const value = payload.benchmarks?.[key];
    const card = node("article", "session-evaluation-result");
    card.append(node("strong", "", label));
    if (!value?.vad?.available) {
      card.append(node("span", "", "尚未计算"));
    } else {
      card.append(
        node("span", "", `F1 ${metric(value.vad.f1)}`),
        node("span", "", `召回 ${metric(value.vad.recall)}`),
        node("span", "", `精确率 ${metric(value.vad.precision)}`),
        node("span", "", `误报率 ${metric(value.vad.false_alarm_rate)}`),
        node("small", "", `报告 #${value.benchmark_run_id}`),
      );
    }
    results.append(card);
  });
  container.append(results);
}

async function createSessionEvaluation(button) {
  button.disabled = true;
  button.textContent = "正在生成…";
  try {
    await api("/api/session-evaluation/v2d1", {
      method: "POST",
      body: JSON.stringify(sessionBody({run_id: state.sessionEvaluation.v2d1_run_id})),
    });
    state.sessionEvaluation = await api(`/api/session-evaluation?${sessionQuery()}`);
    renderSessionEvaluation();
    toast("D.1 人工判断已冻结为连续真值，VAD 对比报告已生成");
  } catch (error) {
    button.disabled = false;
    button.textContent = "生成连续评测真值";
    toast(error.message, "error");
  }
}

export async function loadEvaluation() {
  const empty = $("#evaluation-empty");
  const list = $("#segment-list");
  if (!state.evaluationName) {
    state.evaluation = null;
    empty.classList.toggle("hidden", !state.recordingId);
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

export function renderEvaluationSegments() {
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

export function renderAudioPlayer(label, source, ariaLabel, variant) {
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
  // 评测进度在标注页内展示；顶部指标固定用于 V2 主链状态。
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

export async function runEvaluation() {
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
    await reloadWorkspace();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "生成评测报告";
  }
}

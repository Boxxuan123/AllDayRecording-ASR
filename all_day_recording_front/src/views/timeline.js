import { api } from '../api/client.js'
import { seekAudio, waitForAudioMetadata } from '../audio/playback.js'
import { sessionBody, sessionQuery, state } from '../state/workspace.js'
import { formatDuration, formatOffset } from '../utils/format.js'
import { $, $$, node, toast } from '../workspace/dom.js'
import { reloadWorkspace } from '../workspace/reload.js'
import { renderSessionOptions } from './sessions.js'

const possibleFocusPlayback = new WeakMap()
const manualIdentityPlayback = new WeakMap()

export function renderTimelineOverview() {
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
      node("strong", "", enhancementStatusLabel("V2-D.1", refinement)),
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
  const review = refinement.review;
  if (review?.candidate_count) {
    const reviewPanel = node("div", `possible-review-progress ${review.completed ? "completed" : ""}`);
    const copy = review.completed
      ? `人工检查已完成 · ${review.reviewed_count}/${review.candidate_count}`
      : `人工检查 ${review.reviewed_count}/${review.candidate_count} · 剩余 ${review.pending_count}`;
    reviewPanel.append(node("strong", "", copy));
    const track = node("span", "possible-review-progress-track");
    const fill = node("span", "possible-review-progress-fill");
    fill.style.width = `${review.candidate_count ? (review.reviewed_count / review.candidate_count) * 100 : 0}%`;
    track.append(fill);
    reviewPanel.append(track);
    const complete = node(
      "button",
      "possible-review-complete-button",
      review.completed ? "✓ 本次检查已完成" : review.pending_count ? `完成检查（还剩 ${review.pending_count}）` : "完成本次检查",
    );
    complete.type = "button";
    complete.disabled = review.completed || review.pending_count > 0;
    complete.addEventListener("click", () => completePossibleSpeechReview(complete));
    reviewPanel.append(complete);
    container.append(reviewPanel);
  }
}

function renderTimelineV2D2Status() {
  const container = $("#timeline-v2d2-status");
  const auditPanel = $("#timeline-identity-audit");
  const audit = state.timeline.v2d2;
  container.replaceChildren();
  auditPanel.replaceChildren();
  if (!audit?.available) {
    container.classList.add("unavailable");
    const setup = audit?.setup || {};
    const statusTitle = setup.can_run
      ? "V2-D.2 · 可以运行"
      : setup.identity_pending_count
        ? "V2-D.2 · 等待人物标签"
        : enhancementStatusLabel("V2-D.2", audit);
    container.append(
      node("strong", "", statusTitle),
      node("span", "", audit?.reason || "当前没有人工身份污染审计。"),
    );
    if (setup.confirmed_speech_count) {
      container.append(node(
        "span",
        "timeline-v2d2-progress",
        `人物真值 ${setup.identity_labeled_count || 0}/${setup.confirmed_speech_count}`,
      ));
      if (setup.identity_pending_count) {
        const labelButton = node("button", "enhancement-action-button", "去标人物");
        labelButton.type = "button";
        labelButton.addEventListener("click", openNextIdentityLabel);
        container.append(labelButton);
      }
      if (setup.can_run) {
        const runButton = node("button", "enhancement-action-button primary", "运行 D.2 污染审计");
        runButton.type = "button";
        runButton.addEventListener("click", () => runV2D2Audit(runButton));
        container.append(runButton);
      }
    }
    if (setup.manual_identity_count) {
      container.append(node(
        "span",
        "timeline-v2d2-progress",
        `普通语音身份 ${setup.manual_identity_count} 段 · ${formatDuration(setup.manual_identity_duration_ms || 0)}`,
      ));
    }
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
  const setup = audit.setup || {};
  if (setup.manual_identity_count) {
    container.append(node(
      "span",
      "timeline-v2d2-progress",
      `普通语音身份 ${setup.manual_identity_count} 段 · ${formatDuration(setup.manual_identity_duration_ms || 0)}`,
    ));
  }
  if (setup.needs_rerun) {
    const rerun = node("button", "enhancement-action-button primary", "用新增真值重跑 D.2");
    rerun.type = "button";
    rerun.addEventListener("click", () => runV2D2Audit(rerun));
    container.append(rerun);
  }
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

function openNextIdentityLabel() {
  const item = state.timeline.queues.possible.items.find(
    (candidate) => candidate.review_status === "confirmed_speech" && !candidate.identity_label,
  );
  if (!item) {
    toast("确认语音都已经有人物标签。", "error");
    return;
  }
  state.timelineSelectedId = item.id;
  selectTimelineQueue("possible", {preserveSelection: true});
  toast("已定位到下一条需要人物标签的确认语音");
}

async function runV2D2Audit(button) {
  button.disabled = true;
  button.textContent = "正在审计…";
  try {
    await api("/api/speaker-timeline/v2d2", {
      method: "POST",
      body: JSON.stringify(sessionBody({run_id: state.timeline.v2d1.run_id})),
    });
    await reloadWorkspace();
    toast("V2-D.2 人工身份污染审计已完成");
  } catch (error) {
    button.disabled = false;
    button.textContent = "运行 D.2 污染审计";
    toast(error.message, "error");
  }
}

function renderTimelineV2D3Status() {
  const container = $("#timeline-v2d3-status");
  const expansion = state.timeline.v2d3;
  container.replaceChildren();
  if (!expansion?.available) {
    container.classList.add("unavailable");
    const expansionStatus = expansion?.can_run
      ? "V2-D.3 · 可以运行"
      : state.timeline.v2d2?.available
        ? "V2-D.3 · 等待负对照"
        : "V2-D.3 · 等待 D.2";
    container.append(
      node("strong", "", expansionStatus),
      node("span", "", expansion?.reason || "当前没有身份扩样候选。"),
    );
    if (expansion?.can_run) {
      const select = node("select", "enhancement-target-select");
      expansion.target_identities.forEach((identity) => {
        const option = node("option", "", identityLabel(identity));
        option.value = identity;
        select.append(option);
      });
      const button = node("button", "enhancement-action-button primary", "生成 D.3 扩样候选");
      button.type = "button";
      button.addEventListener("click", () => startV2D3Mining(button, select.value));
      container.append(select, button);
    }
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
  if (expansion.can_run && expansion.target_identities?.length > 1) {
    const controls = node("div", "enhancement-rerun-controls");
    controls.append(node("span", "", "换一个目标人物继续扩样"));
    const select = node("select", "enhancement-target-select");
    expansion.target_identities.forEach((identity) => {
      const current = identity === expansion.target_identity;
      const option = node(
        "option",
        "",
        `${identityLabel(identity)}${current ? "（当前结果）" : "（尚未扩样）"}`,
      );
      option.value = identity;
      if (!current) option.selected = true;
      select.append(option);
    });
    const button = node("button", "enhancement-action-button primary");
    button.type = "button";
    const updateLabel = () => {
      button.textContent = `为${identityLabel(select.value)}生成 D.3 候选`;
    };
    select.addEventListener("change", updateLabel);
    button.addEventListener("click", () => startV2D3Mining(button, select.value));
    updateLabel();
    controls.append(select, button);
    container.append(controls);
  }
}

async function startV2D3Mining(button, targetIdentity) {
  button.disabled = true;
  button.textContent = "正在启动…";
  try {
    const job = await api("/api/speaker-timeline/v2d3", {
      method: "POST",
      body: JSON.stringify(sessionBody({target_identity: targetIdentity})),
    });
    button.textContent = "模型运行中…";
    pollEnhancementJob(job.id, button);
  } catch (error) {
    button.disabled = false;
    button.textContent = "生成 D.3 扩样候选";
    toast(error.message, "error");
  }
}

async function pollEnhancementJob(jobId, button) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    button.textContent = job.detail || "模型运行中…";
    if (job.status === "completed") {
      await reloadWorkspace();
      toast(`V2-D.3 已生成 ${job.result?.selected_candidates || 0} 个身份扩样候选`);
      return;
    }
    if (job.status === "failed") {
      button.disabled = false;
      button.textContent = "重新运行 D.3";
      toast(job.error || "V2-D.3 运行失败", "error");
      return;
    }
    state.jobTimer = window.setTimeout(() => pollEnhancementJob(jobId, button), 1500);
  } catch (error) {
    button.disabled = false;
    button.textContent = "重新运行 D.3";
    toast(error.message, "error");
  }
}

function enhancementStatusLabel(name, value) {
  const labels = {
    completed: "已完成",
    not_applicable: "已跳过",
    needs_review: "等待人工",
    failed: "运行失败",
    running: "运行中",
    not_run: "旧流程未运行",
  };
  return `${name} · ${labels[value?.status] || "未运行"}`;
}

export function selectTimelineQueue(queueName, options = {}) {
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
    if (item.kind === "possible" && item.review_status) {
      facts.append(node("span", `possible-review-badge ${item.review_status}`, possibleReviewLabel(item.review_status)));
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
      `/api/speaker-timeline/window?${sessionQuery()}`
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
  if (item.kind === "possible") {
    detail.append(renderPossibleReviewControls(item, payload));
  }
  if (item.kind !== "possible" && item.kind !== "identity_expansion") {
    detail.append(renderManualIdentityControls(item, payload));
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

function renderManualIdentityControls(item, payload) {
  const wrapper = node("section", "manual-identity-controls");
  const heading = node("div", "manual-identity-heading");
  heading.append(
    node("strong", "", "人物真值采样"),
    node("span", "", "多人窗口中只开放单人 speaker 区间"),
  );
  wrapper.append(heading);

  const cleanRanges = manualIdentityCleanRanges(payload.turns);
  if (!cleanRanges.length) {
    wrapper.append(node(
      "p",
      "manual-identity-unavailable",
      "这个候选没有至少 1 秒的单人说话区间，请换一个候选；这里不开放人物标注。",
    ));
    wrapper.append(renderManualIdentityList(item, payload));
    return wrapper;
  }

  if (!item.manual_identity_selection
    || !manualIdentitySelectionRange(item.manual_identity_selection, cleanRanges)) {
    const cleanTurn = cleanRanges[0];
    item.manual_identity_selection = {
      start_ms: cleanTurn.start_ms,
      end_ms: Math.min(cleanTurn.end_ms, cleanTurn.start_ms + 3000),
    };
  }
  const selection = item.manual_identity_selection;
  const selectionCopy = node("p", "manual-identity-selection-copy");
  const validationCopy = node("p", "manual-identity-validation");
  const suggestionButtons = [];
  let identityActions = null;
  let preview = null;

  const refreshSelection = (startInput, endInput) => {
    updateManualIdentitySelection(
      selection, payload, startInput, endInput, selectionCopy,
    );
    const selectedRange = manualIdentitySelectionRange(selection, cleanRanges);
    validationCopy.classList.toggle("valid", Boolean(selectedRange));
    validationCopy.classList.toggle("invalid", !selectedRange);
    validationCopy.textContent = selectedRange
      ? `单人可标：${selectedRange.speaker}，可以试听后选择人物。`
      : "当前范围跨越了其他 speaker；请选择上方单人区间后再标注。";
    if (identityActions) {
      identityActions.querySelectorAll("button").forEach((button) => {
        button.disabled = !selectedRange;
      });
    }
    suggestionButtons.forEach(({button, suggestion}) => {
      button.classList.toggle("active", (
        selection.start_ms === suggestion.start_ms
        && selection.end_ms === suggestion.end_ms
      ));
    });
  };

  const suggestions = node("div", "manual-identity-suggestions");
  const suggestedRanges = cleanRanges.slice(0, 6).map((range, index) => ({
    start_ms: range.start_ms,
    end_ms: Math.min(range.end_ms, range.start_ms + 3000),
    speaker: range.speaker,
    recommended: index === 0,
  }));
  const suggestionStates = suggestedRanges.map((suggestion) => (
    manualIdentitySuggestionState(suggestion)
  ));
  const labeledCount = suggestionStates.filter((status) => status.labeled).length;
  suggestions.append(node(
    "span",
    "",
    `单人可标区间 · 当前候选已标 ${labeledCount}/${suggestedRanges.length}`,
  ));
  suggestedRanges.forEach((suggestion, index) => {
    const status = suggestionStates[index];
    const prefix = suggestion.recommended ? "推荐 · " : "";
    const button = node(
      "button",
      `manual-identity-suggestion ${status.labeled ? "labeled" : "unlabeled"}`,
      `${prefix}${formatOffset(suggestion.start_ms)}–${formatOffset(suggestion.end_ms)}`
        + ` · ${suggestion.speaker} · ${status.label}`,
    );
    button.type = "button";
    button.addEventListener("click", () => {
      const changed = (
        selection.start_ms !== suggestion.start_ms
        || selection.end_ms !== suggestion.end_ms
      );
      selection.start_ms = suggestion.start_ms;
      selection.end_ms = suggestion.end_ms;
      refreshSelection(startField.input, endField.input);
      if (changed && preview) {
        void playManualIdentitySelection(selection, payload, preview);
      }
    });
    if (suggestion.recommended) button.classList.add("recommended");
    suggestionButtons.push({button, suggestion});
    suggestions.append(button);
  });
  wrapper.append(suggestions);

  const fields = node("div", "manual-identity-range-fields");
  const startField = manualIdentityRangeField(
    "本段起点（秒）",
    (selection.start_ms - payload.start_ms) / 1000,
    (value, commit) => {
      selection.start_ms = Math.round(payload.start_ms + value * 1000);
      refreshSelection(
        commit ? startField.input : null,
        commit ? endField.input : null,
      );
    },
  );
  const endField = manualIdentityRangeField(
    "本段终点（秒）",
    (selection.end_ms - payload.start_ms) / 1000,
    (value, commit) => {
      selection.end_ms = Math.round(payload.start_ms + value * 1000);
      refreshSelection(
        commit ? startField.input : null,
        commit ? endField.input : null,
      );
    },
  );
  fields.append(startField.wrapper, endField.wrapper);
  wrapper.append(fields, selectionCopy);

  const tools = node("div", "manual-identity-tools");
  const capture = node("button", "secondary-button", "截取刚才 3 秒");
  capture.type = "button";
  capture.addEventListener("click", () => {
    const audio = $("#timeline-detail audio");
    if (!audio) return;
    const relativeEndMs = Math.round(Math.max(1000, audio.currentTime * 1000));
    selection.end_ms = Math.min(payload.end_ms, payload.start_ms + relativeEndMs);
    selection.start_ms = Math.max(payload.start_ms, selection.end_ms - 3000);
    if (selection.end_ms - selection.start_ms < 1000) {
      selection.end_ms = Math.min(payload.end_ms, selection.start_ms + 3000);
    }
    refreshSelection(startField.input, endField.input);
  });
  preview = node("button", "manual-identity-preview-button", "▶ 试听所选区间");
  preview.type = "button";
  preview.addEventListener("click", () => playManualIdentitySelection(selection, payload, preview));
  tools.append(capture, preview);
  wrapper.append(tools);

  wrapper.append(validationCopy);
  identityActions = node("div", "manual-identity-actions");
  [
    ["self", "标为本人"],
    ["mother", "标为母亲"],
    ["father", "标为父亲"],
    ["tv", "标为电视/媒体"],
    ["other", "标为其他人物"],
    ["unknown", "无法确定"],
  ].forEach(([identity, label]) => {
    const button = node("button", "manual-identity-save-button", label);
    button.type = "button";
    button.addEventListener("click", () => saveManualIdentitySelection(
      item, payload, selection, identity, identityActions,
    ));
    identityActions.append(button);
  });
  wrapper.append(identityActions);
  refreshSelection(startField.input, endField.input);
  wrapper.append(node(
    "p",
    "manual-identity-guidance",
    "切换到不同区间会自动试听一次；重复点击当前区间不会重播。需要重听可用“试听所选区间”。",
  ));
  wrapper.append(renderManualIdentityList(item, payload));
  return wrapper;
}

function manualIdentityCleanRanges(turns) {
  const bySpeaker = new Map();
  turns.forEach((turn) => {
    const speaker = String(turn.speaker || "");
    if (!speaker || turn.end_ms <= turn.start_ms) return;
    if (!bySpeaker.has(speaker)) bySpeaker.set(speaker, []);
    bySpeaker.get(speaker).push({
      start_ms: Number(turn.start_ms),
      end_ms: Number(turn.end_ms),
    });
  });
  const mergedBySpeaker = new Map();
  bySpeaker.forEach((ranges, speaker) => {
    const merged = [];
    ranges.sort((left, right) => left.start_ms - right.start_ms).forEach((range) => {
      const previous = merged[merged.length - 1];
      if (previous && range.start_ms <= previous.end_ms) {
        previous.end_ms = Math.max(previous.end_ms, range.end_ms);
      } else {
        merged.push({...range});
      }
    });
    mergedBySpeaker.set(speaker, merged);
  });

  const clean = [];
  mergedBySpeaker.forEach((ranges, speaker) => {
    const blockers = [];
    mergedBySpeaker.forEach((otherRanges, otherSpeaker) => {
      if (otherSpeaker !== speaker) blockers.push(...otherRanges);
    });
    ranges.forEach((range) => {
      let fragments = [range];
      blockers.forEach((blocker) => {
        fragments = fragments.flatMap((fragment) => {
          if (blocker.end_ms <= fragment.start_ms || blocker.start_ms >= fragment.end_ms) {
            return [fragment];
          }
          const pieces = [];
          if (blocker.start_ms > fragment.start_ms) {
            pieces.push({...fragment, end_ms: blocker.start_ms});
          }
          if (blocker.end_ms < fragment.end_ms) {
            pieces.push({...fragment, start_ms: blocker.end_ms});
          }
          return pieces;
        });
      });
      fragments
        .filter((fragment) => fragment.end_ms - fragment.start_ms >= 1000)
        .forEach((fragment) => clean.push({...fragment, speaker}));
    });
  });
  return clean.sort((left, right) => (
    (right.end_ms - right.start_ms) - (left.end_ms - left.start_ms)
    || left.start_ms - right.start_ms
  ));
}

function manualIdentitySelectionRange(selection, cleanRanges) {
  return cleanRanges.find((range) => (
    selection.start_ms >= range.start_ms && selection.end_ms <= range.end_ms
  ));
}

function manualIdentitySuggestionState(suggestion) {
  const matches = (state.timeline.manual_identity?.items || []).filter((annotation) => {
    if (annotation.anonymous_speaker_label !== suggestion.speaker) return false;
    const overlapMs = Math.max(
      0,
      Math.min(annotation.end_ms, suggestion.end_ms)
        - Math.max(annotation.start_ms, suggestion.start_ms),
    );
    const shorterMs = Math.min(
      annotation.end_ms - annotation.start_ms,
      suggestion.end_ms - suggestion.start_ms,
    );
    return shorterMs > 0 && overlapMs / shorterMs >= 0.8;
  });
  const identities = [...new Set(matches.map((annotation) => (
    annotation.identity_label
  )))];
  if (!identities.length) return {labeled: false, label: "未标"};
  if (identities.length === 1) {
    return {labeled: true, label: `✓ ${identityLabel(identities[0])}`};
  }
  return {labeled: true, label: "⚠ 多个标签"};
}

function manualIdentityRangeField(label, value, onChange) {
  const wrapper = node("label", "manual-identity-range-field");
  wrapper.append(node("span", "", label));
  const input = node("input");
  input.type = "number";
  input.min = "0";
  input.step = "0.1";
  input.value = Number(value).toFixed(1);
  input.addEventListener("input", () => {
    const current = Number(input.value);
    if (input.value !== "" && Number.isFinite(current)) onChange(current, false);
  });
  input.addEventListener("change", () => onChange(Number(input.value), true));
  wrapper.append(input);
  return {wrapper, input};
}

function updateManualIdentitySelection(selection, payload, startInput, endInput, copy) {
  selection.start_ms = Math.max(payload.start_ms, Math.min(selection.start_ms, payload.end_ms - 1000));
  selection.end_ms = Math.min(payload.end_ms, Math.max(selection.end_ms, selection.start_ms + 1000));
  if (selection.end_ms - selection.start_ms > 10000) selection.end_ms = selection.start_ms + 10000;
  if (startInput) startInput.value = ((selection.start_ms - payload.start_ms) / 1000).toFixed(1);
  if (endInput) endInput.value = ((selection.end_ms - payload.start_ms) / 1000).toFixed(1);
  copy.textContent = `会话时间 ${formatOffset(selection.start_ms)}–${formatOffset(selection.end_ms)}`
    + ` · 共 ${((selection.end_ms - selection.start_ms) / 1000).toFixed(1)} 秒`;
}

async function playManualIdentitySelection(selection, payload, button) {
  const audio = $("#timeline-detail audio");
  if (!audio) return;
  const startSeconds = (selection.start_ms - payload.start_ms) / 1000;
  const endSeconds = (selection.end_ms - payload.start_ms) / 1000;
  const previous = manualIdentityPlayback.get(audio);
  if (previous) previous.cleanup();
  let cancelled = false;
  const preparation = {
    cleanup: () => {
      cancelled = true;
      audio.pause();
      button.disabled = false;
      button.textContent = "▶ 试听所选区间";
      if (manualIdentityPlayback.get(audio) === preparation) {
        manualIdentityPlayback.delete(audio);
      }
    },
  };
  manualIdentityPlayback.set(audio, preparation);
  button.disabled = true;
  button.textContent = "正在准备所选区间…";
  try {
    await waitForAudioMetadata(audio);
    audio.pause();
    await seekAudio(audio, startSeconds);
  } catch (error) {
    preparation.cleanup();
    toast(`无法定位所选区间：${error.message}`, "error");
    return;
  }
  if (cancelled || !audio.isConnected) return;
  let stopTimer = null;
  const cleanup = () => {
    cancelled = true;
    if (stopTimer !== null) window.clearInterval(stopTimer);
    audio.pause();
    audio.removeEventListener("timeupdate", stop);
    audio.removeEventListener("ended", cleanup);
    if (manualIdentityPlayback.get(audio)?.cleanup === cleanup) {
      manualIdentityPlayback.delete(audio);
    }
    button.disabled = false;
    button.textContent = "▶ 试听所选区间";
  };
  const stop = () => {
    if (audio.currentTime >= endSeconds - 0.03) {
      audio.currentTime = endSeconds;
      cleanup();
    }
  };
  audio.currentTime = startSeconds;
  audio.addEventListener("timeupdate", stop);
  audio.addEventListener("ended", cleanup);
  manualIdentityPlayback.set(audio, {cleanup});
  button.disabled = false;
  button.textContent = `正在播放 ${startSeconds.toFixed(1)}–${endSeconds.toFixed(1)}s`;
  try {
    await audio.play();
    if (cancelled) return;
    stopTimer = window.setInterval(stop, 40);
  } catch (error) {
    cleanup();
    toast(`无法试听所选区间：${error.message}`, "error");
  }
}

async function saveManualIdentitySelection(item, payload, selection, identity, actions) {
  actions.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const result = await api("/api/speaker-timeline/manual-identity", {
      method: "POST",
      body: JSON.stringify(sessionBody({
        run_id: state.timeline.run.id,
        start_ms: selection.start_ms,
        end_ms: selection.end_ms,
        identity_label: identity,
      })),
    });
    state.timeline.manual_identity = result.overview;
    updateV2D2SetupFromManualIdentity(result.overview);
    renderTimelineV2D2Status();
    renderTimelineDetail(item, payload);
    toast(`已保存 ${formatOffset(selection.start_ms)}–${formatOffset(selection.end_ms)} 为${identityLabel(identity)}`);
  } catch (error) {
    actions.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    toast(error.message, "error");
  }
}

function updateV2D2SetupFromManualIdentity(overview) {
  const setup = state.timeline.v2d2.setup || {};
  const d1Counts = state.timeline.v2d1.review?.identity_counts || {};
  const combined = {...d1Counts};
  Object.entries(overview.identity_counts || {}).forEach(([identity, count]) => {
    combined[identity] = (combined[identity] || 0) + count;
  });
  state.timeline.v2d2.setup = {
    ...setup,
    identity_counts: combined,
    manual_identity_count: overview.count,
    manual_identity_duration_ms: overview.duration_ms,
    needs_rerun: true,
  };
}

function renderManualIdentityList(item, payload) {
  const container = node("div", "manual-identity-list");
  const rows = state.timeline.manual_identity?.items || [];
  if (!rows.length) {
    container.append(node("span", "", "当前还没有从普通语音保存的人物区间。"));
    return container;
  }
  container.append(node("strong", "", `已保存 ${rows.length} 个普通语音人物区间`));
  rows.slice().reverse().slice(0, 8).forEach((annotation) => {
    const row = node("div", "manual-identity-list-item");
    row.append(node(
      "span",
      "",
      `${formatOffset(annotation.start_ms)}–${formatOffset(annotation.end_ms)}`
        + ` · ${identityLabel(annotation.identity_label)} · ${annotation.anonymous_speaker_label}`,
    ));
    const remove = node("button", "manual-identity-retract-button", "撤回");
    remove.type = "button";
    remove.addEventListener("click", () => retractManualIdentity(annotation, item, payload, remove));
    row.append(remove);
    container.append(row);
  });
  return container;
}

async function retractManualIdentity(annotation, item, payload, button) {
  button.disabled = true;
  try {
    const result = await api("/api/speaker-timeline/manual-identity/retract", {
      method: "POST",
      body: JSON.stringify(sessionBody({
        run_id: state.timeline.run.id,
        annotation_id: annotation.id,
      })),
    });
    state.timeline.manual_identity = result.overview;
    updateV2D2SetupFromManualIdentity(result.overview);
    renderTimelineV2D2Status();
    renderTimelineDetail(item, payload);
    toast("人工人物区间已撤回；历史冻结真值保持不变");
  } catch (error) {
    button.disabled = false;
    toast(error.message, "error");
  }
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
    ["rejected", `不是${identityLabel(item.target_identity)} / 不是语音`],
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
      body: JSON.stringify(sessionBody({
        run_id: state.timeline.v2d3.run_id,
        candidate_id: item.id,
        status,
      })),
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

function renderPossibleReviewControls(item, payload) {
  const wrapper = node("section", "possible-review-controls");
  const clipDurationSeconds = (item.end_ms - item.start_ms) / 1000;
  const focusStartSeconds = (item.focus_start_ms - item.start_ms) / 1000;
  const focusEndSeconds = (item.focus_end_ms - item.start_ms) / 1000;
  const heading = node("div", "possible-review-heading");
  heading.append(
    node("strong", "", "试听后判断琥珀色区间是否确实有人声"),
    node("span", "", item.review_status ? `当前：${possibleReviewLabel(item.review_status)}` : "尚未判断"),
  );
  wrapper.append(heading);

  const focus = node("div", "possible-focus-card");
  const focusCopy = node("div", "possible-focus-copy");
  focusCopy.append(
    node("strong", "", `琥珀区间：本段第 ${focusStartSeconds.toFixed(1)}–${focusEndSeconds.toFixed(1)} 秒`),
    node("span", "", `会话时间 ${formatOffset(item.focus_start_ms)}–${formatOffset(item.focus_end_ms)} · 共 ${((item.focus_end_ms - item.focus_start_ms) / 1000).toFixed(1)} 秒`),
  );
  const focusButton = node("button", "possible-focus-play-button", "▶ 只听琥珀区间");
  focusButton.type = "button";
  focusButton.addEventListener("click", () => playPossibleFocus(item, focusButton));
  focus.append(focusCopy, focusButton);

  const ruler = node("div", "possible-focus-ruler");
  const rulerTrack = node("span", "possible-focus-ruler-track");
  const marker = node("span", "possible-focus-ruler-marker");
  marker.style.left = `${clipDurationSeconds ? (focusStartSeconds / clipDurationSeconds) * 100 : 0}%`;
  marker.style.width = `${clipDurationSeconds ? ((focusEndSeconds - focusStartSeconds) / clipDurationSeconds) * 100 : 0}%`;
  marker.title = `琥珀区间：${focusStartSeconds.toFixed(1)}–${focusEndSeconds.toFixed(1)} 秒`;
  rulerTrack.append(marker);
  ruler.append(
    node("span", "", "0.0s"),
    rulerTrack,
    node("span", "", `${clipDurationSeconds.toFixed(1)}s`),
  );
  focus.append(ruler);
  wrapper.append(focus);

  const actions = node("div", "possible-review-actions");
  [
    ["confirmed_speech", "确认是语音"],
    ["rejected", "确认不是语音"],
    ["uncertain", "听不清"],
  ].forEach(([status, label]) => {
    const button = node("button", `possible-review-button ${status} ${item.review_status === status ? "active" : ""}`, label);
    button.type = "button";
    button.addEventListener("click", () => submitPossibleSpeechReview(item, status, payload, actions));
    actions.append(button);
  });
  wrapper.append(actions);
  if (item.review_status === "confirmed_speech") {
    const identity = node("div", "possible-identity-controls");
    identity.append(
      node("strong", "", "这是谁的声音？"),
      node(
        "span",
        "",
        item.identity_label ? `当前：${identityLabel(item.identity_label)}` : "D.2 需要这项人工真值",
      ),
    );
    const identityActions = node("div", "possible-identity-actions");
    [
      ["self", "本人"],
      ["mother", "母亲"],
      ["father", "父亲"],
      ["tv", "电视/媒体"],
      ["other", "其他人物"],
      ["unknown", "无法确定"],
    ].forEach(([value, label]) => {
      const button = node(
        "button",
        `possible-identity-button ${item.identity_label === value ? "active" : ""}`,
        label,
      );
      button.type = "button";
      button.addEventListener("click", () => submitPossibleIdentity(item, value, payload, identityActions));
      identityActions.append(button);
    });
    identity.append(identityActions);
    wrapper.append(identity);
  }
  wrapper.append(node("p", "", "人工判断单独保存，不会修改 V2-D.1 模型输出。三种结论都计入审核进度。"));
  return wrapper;
}

async function submitPossibleIdentity(item, identityLabelValue, payload, actions) {
  actions.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const result = await api("/api/speaker-timeline/possible-identity", {
      method: "POST",
      body: JSON.stringify(sessionBody({
        run_id: state.timeline.v2d1.run_id,
        candidate_id: item.id,
        identity_label: identityLabelValue,
      })),
    });
    item.identity_label = result.identity_label;
    state.timeline.v2d1.review = result.review;
    state.timeline.queues.possible.review = result.review;
    state.timeline.v2d2.setup = {
      confirmed_speech_count: result.review.confirmed_speech_count,
      identity_labeled_count: result.review.identity_labeled_count,
      identity_pending_count: result.review.identity_pending_count,
      identity_counts: result.review.identity_counts,
      can_run: result.review.completed
        && result.review.confirmed_speech_count > 0
        && result.review.identity_pending_count === 0,
    };
    renderTimelineCandidates();
    renderTimelineV2D1Status();
    renderTimelineV2D2Status();
    renderTimelineDetail(item, payload);
    toast(`${item.title}：人物已标为${identityLabel(result.identity_label)}`);
  } catch (error) {
    actions.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    toast(error.message, "error");
  }
}

function playPossibleFocus(item, button) {
  const audio = $("#timeline-detail audio");
  if (!audio) {
    toast("播放器尚未准备好，请稍后再试。", "error");
    return;
  }
  const focusStartSeconds = Math.max(0, (item.focus_start_ms - item.start_ms) / 1000);
  const focusEndSeconds = Math.max(focusStartSeconds, (item.focus_end_ms - item.start_ms) / 1000);
  const previous = possibleFocusPlayback.get(audio);
  if (previous) {
    audio.removeEventListener("timeupdate", previous.stop);
    audio.removeEventListener("ended", previous.cleanup);
    previous.cleanup();
  }

  const cleanup = () => {
    audio.removeEventListener("timeupdate", stop);
    audio.removeEventListener("ended", cleanup);
    possibleFocusPlayback.delete(audio);
    button.classList.remove("playing");
    button.textContent = "▶ 只听琥珀区间";
  };
  const stop = () => {
    if (audio.currentTime >= focusEndSeconds) {
      audio.pause();
      audio.currentTime = focusEndSeconds;
      cleanup();
    }
  };
  const startPlayback = async () => {
    audio.currentTime = focusStartSeconds;
    audio.addEventListener("timeupdate", stop);
    audio.addEventListener("ended", cleanup);
    possibleFocusPlayback.set(audio, {stop, cleanup});
    button.classList.add("playing");
    button.textContent = `正在播放 ${focusStartSeconds.toFixed(1)}–${focusEndSeconds.toFixed(1)}s`;
    try {
      await audio.play();
    } catch (error) {
      cleanup();
      toast(`无法播放琥珀区间：${error.message}`, "error");
    }
  };
  if (audio.readyState >= 1) {
    startPlayback();
  } else {
    audio.addEventListener("loadedmetadata", startPlayback, {once: true});
    audio.load();
  }
}

async function submitPossibleSpeechReview(item, status, payload, actions) {
  actions.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try {
    const result = await api("/api/speaker-timeline/possible-review", {
      method: "POST",
      body: JSON.stringify(sessionBody({
        run_id: state.timeline.v2d1.run_id,
        candidate_id: item.id,
        status,
      })),
    });
    item.review_status = result.status;
    if (result.status !== "confirmed_speech") item.identity_label = null;
    item.reviewed_at = result.updated_at;
    state.timeline.v2d1.review = result.review;
    state.timeline.queues.possible.review = result.review;
    renderTimelineCandidates();
    renderTimelineV2D1Status();
    renderTimelineDetail(item, payload);
    toast(`${item.title}：${possibleReviewLabel(status)} · 已审 ${result.review.reviewed_count}/${result.review.candidate_count}`);
  } catch (error) {
    actions.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    toast(error.message, "error");
  }
}

async function completePossibleSpeechReview(button) {
  button.disabled = true;
  button.textContent = "正在完成…";
  try {
    const review = await api("/api/speaker-timeline/possible-review/complete", {
      method: "POST",
      body: JSON.stringify(sessionBody({run_id: state.timeline.v2d1.run_id})),
    });
    state.timeline.v2d1.review = review;
    state.timeline.queues.possible.review = review;
    const [sessions, dashboard] = await Promise.all([
      api("/api/sessions"),
      api(`/api/session-dashboard?${sessionQuery()}`),
    ]);
    state.sessions = sessions.sessions;
    state.dashboard = dashboard;
    renderSessionOptions();
    $("#recording-select").value = String(state.sessionId);
    await reloadWorkspace();
    renderTimelineV2D1Status();
    toast("V2-D.1 人工检查已完成，工作流状态已重新计算");
  } catch (error) {
    button.disabled = false;
    button.textContent = "完成本次检查";
    toast(error.message, "error");
  }
}

function possibleReviewLabel(status) {
  return {
    confirmed_speech: "确认是语音",
    rejected: "确认不是语音",
    uncertain: "听不清",
  }[status] || "尚未判断";
}

function identityReviewLabel(status) {
  return {
    confirmed_target: "确认是目标人物",
    rejected: "不是目标 / 非语音",
    uncertain: "听不清",
  }[status] || "尚未判断";
}

export function timelineFact(label, value) {
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
    other: "其他人物",
    unknown: "无法确定",
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

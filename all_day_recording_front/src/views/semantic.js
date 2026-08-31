import { api } from '../api/client.js'
import { releaseAudioWithin, replaceAudioSource } from '../audio/playback.js'
import { sessionBody, sessionQuery, state } from '../state/workspace.js'
import { formatOffset } from '../utils/format.js'
import { $, node, toast } from '../workspace/dom.js'
import { renderAudioPlayer } from './evaluation.js'

export function renderSemantic() {
  const payload = state.semantic;
  const empty = $("#semantic-empty");
  const workspace = $("#semantic-workspace");
  releaseAudioWithin(workspace);
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
  const episodeCount = summary.episode_count ?? summary.conversation_count ?? summary.event_count ?? 0;
  const sceneCount = summary.scene_count ?? 0;
  const claimCount = summary.claim_count ?? summary.facts ?? 0;
  const actionCount = summary.action_count ?? summary.actions ?? 0;
  const unresolvedCount = summary.unresolved_count ?? 0;
  const excludedCount = summary.excluded_block_count ?? 0;
  const jobCount = summary.llm_job_count ?? 0;
  $("#semantic-episode-count").textContent = String(episodeCount);
  $("#semantic-scene-count").textContent = String(sceneCount);
  $("#semantic-claim-count").textContent = String(claimCount);
  $("#semantic-action-count").textContent = String(actionCount);
  $("#semantic-unresolved-count").textContent = String(unresolvedCount);
  $("#semantic-excluded-count").textContent = String(excludedCount);
  $("#semantic-job-count").textContent = jobCount ? String(jobCount) : "旧版";
  $("#semantic-token-count").textContent = Number(summary.token_count || 0).toLocaleString("zh-CN");
  $("#semantic-reviewed-count").textContent = `${payload.reviewed_candidates} / ${payload.candidates.length}`;
  $("#semantic-provider").textContent = payload.run.manual_eval
    ? "Codex 手工回放"
    : payload.run.provider === "local_mock" ? "本地契约 mock" : payload.run.provider;
  $("#semantic-run-note").textContent = (
    `RUN #${payload.run.id} · ASR #${payload.run.asr_run_id}`
    + `${payload.run.diarization_run_id ? ` · DIARIZATION #${payload.run.diarization_run_id}` : ""}`
    + ` · REQUEST ${payload.run.request_sha256.slice(0, 12)}`
  );
  const transport = payload.transport || {};
  $("#semantic-transport-note").textContent = payload.version === "v2-e.0.2"
    ? `${episodeCount} 个上下文 Episode → ${jobCount} 个传输任务 → ${sceneCount} 个语义场景；Episode 无时长硬切，${Math.round((transport.review_clip_ms || 120000) / 1000)} 秒仅是播放器切片。`
    : payload.version === "v2-e.0.1"
      ? `${episodeCount} 个旧版完整对话 → ${jobCount} 个计划请求；重新生成后会升级为四轨 Episode 证据。`
      : "这是旧版 120 秒证据分组；重新生成后会升级为四轨 Episode 证据。";
  renderSemanticEpisodes(payload.episodes || []);
  renderSemanticExcluded(payload.excluded_blocks || []);
  const pending = payload.candidates.filter((item) => !item.review_status).length;
  updateSemanticBadge(pending);
  const container = $("#semantic-candidate-list");
  container.replaceChildren();
  payload.candidates.forEach((candidate) => container.append(renderSemanticCard(candidate)));
}
function renderSemanticEpisodes(items) {
  const panel = $("#semantic-episode-panel");
  const container = $("#semantic-episode-list");
  container.replaceChildren();
  panel.classList.toggle("hidden", items.length === 0);
  items.forEach((item) => {
    const sources = Object.entries(item.source_counts || {})
      .map(([label, count]) => `${label} ${count}`)
      .join(" · ") || "来源未定";
    const identities = Object.entries(item.identity_counts || {})
      .map(([label, count]) => `${label} ${count}`)
      .join(" · ") || "身份未定";
    const contaminated = (item.voice_clusters || [])
      .filter((cluster) => cluster.contaminated)
      .map((cluster) => cluster.label)
      .join("、");
    const row = node("div", "semantic-excluded-item semantic-episode-item");
    row.append(
      node("strong", "", `${formatOffset(item.start_ms)}–${formatOffset(item.end_ms)}`),
      node("span", "", item.transcript_preview || "（无文字预览）"),
      node(
        "small",
        "",
        `${item.utterance_count} 说话轮次 · ${sources} · ${identities}`
          + (contaminated ? ` · 污染声纹 ${contaminated}` : ""),
      ),
    );
    container.append(row);
  });
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
  const unitLabels = {
    day: "DAY SUMMARY",
    scene: "SEMANTIC SCENE",
    claim: "GROUNDED CLAIM",
    action: "ACTION CANDIDATE",
    conversation: "LEGACY CONVERSATION",
  };
  meta.append(node("span", "segment-id", unitLabels[candidate.semantic_unit] || "SEMANTIC EVIDENCE"));
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

  if (candidate.review_clips?.length > 1) {
    card.append(renderSemanticReviewPlayer(candidate));
  } else if (candidate.audio_url) {
    card.append(renderAudioPlayer("本地原音证据", candidate.audio_url, `播放语义证据 ${candidate.id}`, "exact"));
  }
  const evidence = node("div", "semantic-evidence-row");
  if (candidate.type === "daily_summary") {
    evidence.append(node(
      "span",
      "semantic-evidence-chip",
      `${candidate.evidence.scene_ids?.length || candidate.evidence.conversation_keys?.length || candidate.evidence.event_keys?.length || 0} 个场景引用`,
    ));
  } else {
    const uncertainty = candidate.evidence.uncertainty || {};
    evidence.append(
      node("span", "semantic-evidence-chip", `${candidate.evidence.token_ids?.length || 0} tokens`),
      node("span", "semantic-evidence-chip", `${candidate.evidence.utterance_ids?.length || candidate.evidence.utterance_keys?.length || 0} 说话轮次`),
      node("span", "semantic-evidence-chip", `${candidate.review_clips?.length || 1} 个回听切片`),
    );
    if (candidate.evidence.source_labels?.length) {
      evidence.append(node("span", "semantic-evidence-chip", `来源 ${candidate.evidence.source_labels.join(" / ")}`));
    }
    if (candidate.evidence.identity_labels?.length) {
      evidence.append(node("span", "semantic-evidence-chip", `身份 ${candidate.evidence.identity_labels.join(" / ")}`));
    }
    if (uncertainty.unassigned_tokens) {
      evidence.append(node("span", "semantic-evidence-chip warning", `${uncertainty.unassigned_tokens} 无归属`));
    }
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

function renderSemanticReviewPlayer(candidate) {
  const wrapper = node("div", "semantic-review-player");
  const label = node("label", "semantic-clip-select");
  label.append(node("span", "", "本地回听切片（不会改变 Episode 或场景边界）"));
  const select = node("select");
  candidate.review_clips.forEach((clip) => {
    const option = node("option", "", `片段 ${clip.index} · ${formatOffset(clip.start_ms)}–${formatOffset(clip.end_ms)}`);
    option.value = clip.audio_url;
    select.append(option);
  });
  label.append(select);
  const player = renderAudioPlayer("当前切片", candidate.review_clips[0].audio_url, `播放语义证据 ${candidate.id} 的回听切片`, "exact");
  const audio = player.querySelector("audio");
  select.addEventListener("change", () => {
    replaceAudioSource(audio, select.value);
  });
  wrapper.append(label, player);
  return wrapper;
}

export async function generateSemantic() {
  if (!state.sessionId) return;
  const button = $("#generate-semantic-button");
  button.disabled = true;
  button.textContent = "整理证据中…";
  try {
    const result = await api("/api/semantic/generate", {
      method: "POST",
      body: JSON.stringify(sessionBody()),
    });
    toast(`V2-E.0.2 run #${result.run_id}：${result.episode_count} 个 Episode；当前本地 mock 不生成场景`);
    state.semantic = await api(`/api/semantic?${sessionQuery()}`);
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
      body: JSON.stringify(sessionBody({
        status,
        title: title.value,
        body: body.value,
        note: note.value,
      })),
    });
    state.semantic = await api(`/api/semantic?${sessionQuery()}`);
    renderSemantic();
    toast(`语义证据 #${candidate.id} 已${status === "confirmed" ? "确认" : "排除"}`);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

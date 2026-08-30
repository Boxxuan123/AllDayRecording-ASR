import { api } from '../api/client.js'
import { state } from '../state/workspace.js'
import { node, toast, $ } from '../workspace/dom.js'
import { reloadWorkspace } from '../workspace/reload.js'

export function renderActions() {
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
    await reloadWorkspace();
  } catch (error) {
    toast(error.message, "error");
  }
}

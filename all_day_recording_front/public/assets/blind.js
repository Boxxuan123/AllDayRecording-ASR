document.addEventListener("DOMContentLoaded", () => {
  const state = { task: null, windowIndex: 0, editingId: null };
  const $ = (id) => document.getElementById(id);
  const player = $("audio-player");
  const sourceLabels = {
    live_person: "现场人声",
    media_playback: "电视 / 媒体",
    mixed_live_media: "现场 + 媒体重叠",
    unknown: "声源不确定",
  };

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `请求失败：${response.status}`);
    return payload;
  }

  function showNotice(message, error = false) {
    const notice = $("notice");
    notice.textContent = message;
    notice.className = `notice${error ? " error" : ""}`;
    window.clearTimeout(showNotice.timer);
    showNotice.timer = window.setTimeout(() => notice.classList.add("hidden"), 3600);
  }

  function formatMs(value, includeHours = false) {
    let ms = Math.max(0, Math.round(value));
    const hours = Math.floor(ms / 3600000); ms %= 3600000;
    const minutes = Math.floor(ms / 60000); ms %= 60000;
    const seconds = Math.floor(ms / 1000);
    const millis = ms % 1000;
    const base = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
    return includeHours ? `${String(hours).padStart(2, "0")}:${base}` : base;
  }

  function currentWindow() {
    return state.task.windows.find((item) => item.window_index === state.windowIndex);
  }

  async function reload(selectWindow = state.windowIndex) {
    state.task = await api("/api/task");
    state.windowIndex = state.task.windows.some((item) => item.window_index === selectWindow)
      ? selectWindow : state.task.windows[0].window_index;
    render();
  }

  function render() {
    const task = state.task;
    const version = task.protocol?.includes("V2-C.2") ? "V2-C.2" : "V2-C.1";
    $("protocol-label").textContent = `ALLDAY / ${version} / BLIND`;
    const reviewMinutes = (task.review_duration_ms / 60000).toFixed(1);
    $("task-name").textContent = `${task.name} · ${task.windows.length} 块 / 共 ${reviewMinutes} 分钟`;
    $("finalize-help").textContent = `全部 ${task.windows.length} 个音频块都完整听完后填写。最终确认后网页将进入只读状态，再使用 CLI 导入冻结真值。`;
    const complete = task.windows.filter((item) => item.review_status === "complete").length;
    $("progress-label").textContent = `${complete} / ${task.windows.length}`;
    $("progress-bar").style.width = `${(complete / task.windows.length) * 100}%`;
    $("annotation-count").textContent = `${task.utterances.length} 条语音标注`;
    $("legacy-source-help").textContent = task.speech_source_default === "live_person"
      ? "本任务现有旧标注按“现场人声”兼容显示；只有电视、重叠或不确定声源需要额外选择。"
      : `未分类的旧标注按“${sourceLabels[task.speech_source_default] || sourceLabels.unknown}”显示；新标注请明确选择声源。`;
    document.body.classList.toggle("readonly", task.finalized);
    renderWindows();
    renderCurrentWindow();
    if (task.finalized) showNotice("任务已经最终确认，现在是只读状态。");
  }

  function renderWindows() {
    const list = $("window-list");
    list.innerHTML = "";
    state.task.windows.forEach((item) => {
      const button = document.createElement("button");
      button.className = `window-button${item.window_index === state.windowIndex ? " active" : ""}${item.review_status === "complete" ? " complete" : ""}`;
      button.innerHTML = `
        <span class="window-number">${String(item.window_index + 1).padStart(2, "0")}</span>
        <span class="window-copy"><strong>${formatMs(item.session_start_ms, true)}</strong><small>${item.review_status === "complete" ? "已完整检查" : "待检查"}</small></span>
        <span class="window-count">${item.utterance_count}</span>`;
      button.addEventListener("click", () => {
        state.windowIndex = item.window_index;
        state.editingId = null;
        render();
        resetEditor();
      });
      list.appendChild(button);
    });
  }

  function renderCurrentWindow() {
    const item = currentWindow();
    $("window-title").textContent = `音频块 ${String(item.window_index + 1).padStart(2, "0")}`;
    $("window-range").textContent = `会话 ${formatMs(item.session_start_ms, true)}–${formatMs(item.session_end_ms, true)} · ${((item.session_end_ms - item.session_start_ms) / 60000).toFixed(1)} 分钟`;
    if (player.dataset.window !== String(item.window_index)) {
      player.pause();
      player.src = item.audio_url;
      player.dataset.window = String(item.window_index);
      player.load();
    }
    const review = $("review-button");
    const isComplete = item.review_status === "complete";
    review.textContent = isComplete ? "✓ 本块已完整检查" : "标记本块已完整检查";
    review.classList.toggle("complete", isComplete);
    $("start-seconds").max = ((item.session_end_ms - item.session_start_ms) / 1000).toFixed(3);
    $("end-seconds").max = $("start-seconds").max;
    renderUtterances();
    updateTimeReadout();
    updateBoundarySummary();
  }

  function windowUtterances() {
    return state.task.utterances.filter((item) => item.window_index === state.windowIndex);
  }

  function renderUtterances() {
    const utterances = windowUtterances();
    $("window-annotation-count").textContent = `${utterances.length} 条`;
    $("utterance-empty").classList.toggle("hidden", utterances.length > 0);
    const list = $("utterance-list");
    list.innerHTML = "";
    utterances.forEach((item) => {
      const card = document.createElement("article");
      card.className = "utterance-card";
      const text = item.unintelligible ? "[有语音，但听不清]" : item.text;
      const sourceLabel = sourceLabels[item.speech_source] || sourceLabels.unknown;
      card.innerHTML = `
        <button class="utterance-time" title="从这里播放">${formatMs(item.start_ms)} → ${formatMs(item.end_ms)}</button>
        <div class="utterance-copy">
          <span class="source-badge source-${item.speech_source}">${sourceLabel}${item.speech_source_inferred ? " · 旧标注" : ""}</span>
          <div class="utterance-text${item.unintelligible ? " unintelligible" : ""}"></div>
        </div>
        <div class="utterance-actions"><button data-action="edit">编辑</button><button class="danger" data-action="delete">删除</button></div>`;
      card.querySelector(".utterance-text").textContent = text;
      card.querySelector(".utterance-time").addEventListener("click", () => {
        player.currentTime = item.start_ms / 1000;
        player.play();
      });
      card.querySelector('[data-action="edit"]').addEventListener("click", () => editUtterance(item));
      card.querySelector('[data-action="delete"]').addEventListener("click", () => deleteUtterance(item));
      list.appendChild(card);
    });
  }

  function editUtterance(item) {
    state.editingId = item.utterance_id;
    $("editor-title").textContent = "编辑语音";
    $("cancel-edit").classList.remove("hidden");
    $("start-seconds").value = (item.start_ms / 1000).toFixed(3);
    $("end-seconds").value = (item.end_ms / 1000).toFixed(3);
    $("transcript").value = item.text;
    $("unintelligible").checked = item.unintelligible;
    setSpeechSource(item.speech_source);
    syncTranscriptMode();
    player.currentTime = item.start_ms / 1000;
    updateBoundarySummary();
    $("transcript").focus();
  }

  function resetEditor(keepEndAsStart = false) {
    const previousEnd = Number($("end-seconds").value || 0);
    state.editingId = null;
    $("editor-title").textContent = "新增语音";
    $("cancel-edit").classList.add("hidden");
    $("start-seconds").value = (keepEndAsStart ? previousEnd : player.currentTime).toFixed(3);
    $("end-seconds").value = (keepEndAsStart ? previousEnd : player.currentTime).toFixed(3);
    $("transcript").value = "";
    $("unintelligible").checked = false;
    setSpeechSource("live_person");
    syncTranscriptMode();
    updateBoundarySummary();
  }

  function syncTranscriptMode() {
    const unintelligible = $("unintelligible").checked;
    if (unintelligible) $("transcript").value = "";
    $("transcript").disabled = unintelligible;
  }

  function setSpeechSource(value) {
    const option = document.querySelector(`input[name="speech-source"][value="${value}"]`);
    (option || document.querySelector('input[name="speech-source"][value="live_person"]')).checked = true;
  }

  function selectedSpeechSource() {
    return document.querySelector('input[name="speech-source"]:checked').value;
  }

  async function saveUtterance() {
    const startMs = Math.round(Number($("start-seconds").value) * 1000);
    const endMs = Math.round(Number($("end-seconds").value) * 1000);
    try {
      await api("/api/utterances", {
        method: "POST",
        body: JSON.stringify({
          utterance_id: state.editingId,
          window_index: state.windowIndex,
          start_ms: startMs,
          end_ms: endMs,
          text: $("transcript").value,
          unintelligible: $("unintelligible").checked,
          speech_source: selectedSpeechSource(),
        }),
      });
      resetEditor(true);
      await reload();
      showNotice("语音标注已保存；本块已自动回到待检查状态。完成整块后再点绿色按钮。");
    } catch (error) { showNotice(error.message, true); }
  }

  async function deleteUtterance(item) {
    if (!window.confirm(`确定删除 ${formatMs(item.start_ms)}–${formatMs(item.end_ms)} 的标注吗？`)) return;
    try {
      await api(`/api/utterances/${item.utterance_id}/delete`, { method: "POST", body: "{}" });
      resetEditor();
      await reload();
      showNotice("标注已删除。");
    } catch (error) { showNotice(error.message, true); }
  }

  async function toggleReview() {
    const item = currentWindow();
    const status = item.review_status === "complete" ? "pending" : "complete";
    try {
      await api(`/api/windows/${item.window_index}/status`, { method: "POST", body: JSON.stringify({ status }) });
      await reload();
      showNotice(status === "complete" ? "本块已标记为完整检查。" : "本块已恢复为待检查。");
    } catch (error) { showNotice(error.message, true); }
  }

  async function finalizeTask() {
    if (!window.confirm(`最终确认后任务会进入只读状态。确定 ${state.task.windows.length} 个音频块都已完整听完并穷尽标注吗？`)) return;
    try {
      await api("/api/finalize", {
        method: "POST",
        body: JSON.stringify({ annotator: $("annotator").value, confirm_unseen: $("confirm-unseen").checked }),
      });
      await reload();
      showNotice("盲标任务已最终确认。下一步运行 benchmark import-truth 冻结入库。");
    } catch (error) { showNotice(error.message, true); }
  }

  function capture(which) {
    $(which === "start" ? "start-seconds" : "end-seconds").value = player.currentTime.toFixed(3);
    updateBoundarySummary();
  }

  function updateBoundarySummary() {
    const start = Number($("start-seconds").value || 0);
    const end = Number($("end-seconds").value || 0);
    $("boundary-summary").textContent = end > start
      ? `${formatMs(start * 1000)} → ${formatMs(end * 1000)} · ${(end - start).toFixed(2)} 秒`
      : "结束时间必须晚于开始时间";
  }

  function updateTimeReadout() {
    const item = currentWindow();
    if (!item) return;
    $("relative-time").textContent = formatMs(player.currentTime * 1000);
    $("session-time").textContent = formatMs(item.session_start_ms + player.currentTime * 1000, true);
  }

  $("capture-start").addEventListener("click", () => capture("start"));
  $("capture-end").addEventListener("click", () => capture("end"));
  $("save-utterance").addEventListener("click", saveUtterance);
  $("cancel-edit").addEventListener("click", () => resetEditor());
  $("review-button").addEventListener("click", toggleReview);
  $("finalize-button").addEventListener("click", finalizeTask);
  $("start-seconds").addEventListener("input", updateBoundarySummary);
  $("end-seconds").addEventListener("input", updateBoundarySummary);
  $("unintelligible").addEventListener("change", syncTranscriptMode);
  player.addEventListener("timeupdate", updateTimeReadout);

  document.addEventListener("keydown", (event) => {
    const editingText = ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName);
    if (event.code === "Space" && !editingText) {
      event.preventDefault();
      player.paused ? player.play() : player.pause();
    } else if (event.key.toLowerCase() === "a" && !editingText) {
      capture("start");
    } else if (event.key.toLowerCase() === "d" && !editingText) {
      capture("end");
    } else if (event.key === "Enter" && event.ctrlKey) {
      event.preventDefault();
      saveUtterance();
    }
  });

  reload().catch((error) => showNotice(error.message, true));
});

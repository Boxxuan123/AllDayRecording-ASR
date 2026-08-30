<template>
  <section id="view-timeline" class="view active-view">
    <div class="section-heading timeline-heading">
      <div>
        <p class="eyebrow">V2-D / SPEAKER TIMELINE</p>
        <h3>先听最可能有人对话的地方</h3>
        <p>系统按多人切换、语音密度、文字和重叠自动排序；原始录音只读，试听文件按需生成。</p>
      </div>
      <div id="timeline-run-chip" class="timeline-run-chip">读取 V2-D…</div>
    </div>

    <div id="timeline-empty" class="empty-state hidden">
      <strong>还没有可看的说话人时间轴</strong>
      <p id="timeline-empty-copy">请先完成 V2-D 说话人运行。</p>
    </div>

    <div id="timeline-workspace" class="timeline-workspace hidden">
      <div class="timeline-summary" aria-label="V2-D 结果概览">
        <article>
          <span>匿名说话人</span>
          <strong id="timeline-speaker-count">—</strong>
        </article>
        <article>
          <span>说话时间</span>
          <strong id="timeline-speech-duration">—</strong>
        </article>
        <article>
          <span>真正重叠</span>
          <strong id="timeline-overlap-duration">—</strong>
        </article>
        <article>
          <span>无归属词</span>
          <strong id="timeline-unassigned-count">—</strong>
        </article>
      </div>

      <div id="speaker-legend" class="speaker-legend" aria-label="说话人图例"></div>
      <div id="timeline-v2d1-status" class="timeline-v2d1-status" aria-live="polite"></div>
      <div id="timeline-v2d2-status" class="timeline-v2d2-status" aria-live="polite"></div>
      <div id="timeline-v2d3-status" class="timeline-v2d3-status" aria-live="polite"></div>
      <section
        id="timeline-identity-audit"
        class="timeline-identity-audit hidden"
        aria-label="身份污染审计"
      ></section>

      <div class="timeline-queue-toolbar">
        <div class="timeline-queue-tabs" role="tablist" aria-label="试听队列">
          <button class="timeline-queue-button active" data-timeline-queue="conversation" role="tab">
            多人对话 <span id="timeline-conversation-count">0</span>
          </button>
          <button class="timeline-queue-button" data-timeline-queue="overlap" role="tab">
            重叠说话 <span id="timeline-overlap-count">0</span>
          </button>
          <button class="timeline-queue-button" data-timeline-queue="unassigned" role="tab">
            无归属文字 <span id="timeline-unassigned-group-count">0</span>
          </button>
          <button class="timeline-queue-button" data-timeline-queue="possible" role="tab">
            可能漏检 <span id="timeline-possible-count">0</span>
          </button>
          <button class="timeline-queue-button" data-timeline-queue="identity_expansion" role="tab">
            身份扩样 <span id="timeline-identity-expansion-count">0</span>
          </button>
        </div>
        <p id="timeline-queue-description">正在整理值得听的片段…</p>
      </div>

      <div class="timeline-review-grid">
        <aside class="timeline-candidate-panel" aria-label="候选片段">
          <div class="timeline-candidate-head">
            <strong id="timeline-queue-title">多人对话</strong>
            <span id="timeline-queue-position">0 / 0</span>
          </div>
          <div id="timeline-candidate-list" class="timeline-candidate-list"></div>
        </aside>

        <article id="timeline-detail" class="timeline-detail">
          <div class="timeline-detail-empty">
            <strong>选择左侧片段开始试听</strong>
            <p>默认已经按信息量排序，不需要从两小时开头听。</p>
          </div>
        </article>
      </div>
    </div>
  </section>
</template>

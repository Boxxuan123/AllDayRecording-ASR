<template>
  <section id="view-semantic" class="view">
    <div class="section-heading semantic-heading">
      <div>
        <p class="eyebrow">V2-E.0.2 / EPISODE EVIDENCE CONTRACT</p>
        <h3>Episode 提供完整上下文，LLM 从中提取真正场景</h3>
        <p>
          Episode 没有 120 秒硬上限，也不等于场景。ASR 文字、匿名声纹、现场/媒体来源和人物身份分别传递；短片段只负责回听。
        </p>
      </div>
      <div class="section-actions">
        <button id="generate-semantic-button" class="secondary-button">生成本地证据包</button>
      </div>
    </div>

    <div class="semantic-privacy-notice">
      <strong>本轮网络访问：关闭</strong>
      <p>
        本地底账保留 token 与原音坐标；provider payload 只含 episode 内的主 ASR 说话轮次、匿名声纹标签、来源/身份区间证据和不确定性，不含音频、路径、原音哈希、数据库 token ID 或声纹向量。
      </p>
    </div>

    <div id="semantic-empty" class="empty-state hidden">
      <strong>还没有语义证据包</strong>
      <p id="semantic-empty-copy">完成 V2-C/V2-D 后即可生成。</p>
    </div>

    <div id="semantic-workspace" class="semantic-workspace hidden">
      <div class="semantic-summary-grid" aria-label="语义证据包概览">
        <article><span>Episode</span><strong id="semantic-episode-count">—</strong></article>
        <article><span>语义场景</span><strong id="semantic-scene-count">—</strong></article>
        <article><span>事实陈述</span><strong id="semantic-claim-count">—</strong></article>
        <article><span>行动候选</span><strong id="semantic-action-count">—</strong></article>
        <article><span>待解决</span><strong id="semantic-unresolved-count">—</strong></article>
        <article><span>不发送块</span><strong id="semantic-excluded-count">—</strong></article>
        <article><span>计划请求</span><strong id="semantic-job-count">—</strong></article>
        <article><span>对齐 token</span><strong id="semantic-token-count">—</strong></article>
        <article><span>人工审核</span><strong id="semantic-reviewed-count">—</strong></article>
        <article><span>Provider</span><strong id="semantic-provider">—</strong></article>
      </div>
      <div id="semantic-run-note" class="semantic-run-note"></div>
      <div id="semantic-transport-note" class="semantic-transport-note"></div>
      <details id="semantic-episode-panel" class="semantic-excluded-panel">
        <summary>查看送入模型的上下文 Episode（它们不是语义场景）</summary>
        <div id="semantic-episode-list" class="semantic-excluded-list"></div>
      </details>
      <details id="semantic-excluded-panel" class="semantic-excluded-panel hidden">
        <summary>查看留在本地、不会发送给 LLM 的低信息证据</summary>
        <div id="semantic-excluded-list" class="semantic-excluded-list"></div>
      </details>
      <div id="semantic-candidate-list" class="semantic-candidate-list"></div>
    </div>
  </section>
</template>

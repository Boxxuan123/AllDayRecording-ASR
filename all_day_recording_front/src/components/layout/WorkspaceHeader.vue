<template>
  <header class="topbar">
    <div>
      <p class="eyebrow">ALL DAY RECORDING / ASR</p>
      <h2 id="page-title">说话人时间轴</h2>
    </div>
    <div class="topbar-actions">
      <label class="select-field">
        <span>录音会话</span>
        <select id="recording-select" aria-label="选择录音会话"></select>
      </label>
      <button id="run-daily-button" class="primary-button">刷新 V2 结果</button>
    </div>
  </header>

  <section class="metric-grid" aria-label="录音概览">
    <article class="metric-card accent-cyan">
      <span>V2-C 主 token</span>
      <strong id="metric-segments">—</strong>
      <small id="metric-segments-detail">读取中</small>
    </article>
    <article class="metric-card accent-violet">
      <span>V2-D 匿名说话人</span>
      <strong id="metric-annotations">—</strong>
      <small id="metric-annotations-detail">读取中</small>
    </article>
    <article class="metric-card accent-amber">
      <span>V2 workflow</span>
      <strong id="metric-evaluation">—</strong>
      <small id="metric-evaluation-detail">尚未选择模板</small>
    </article>
    <article class="metric-card accent-red">
      <span>输入完整性</span>
      <strong id="metric-actions">—</strong>
      <small id="metric-actions-detail">读取中</small>
    </article>
  </section>

  <div id="job-banner" class="job-banner hidden" role="status">
    <span class="pulse-dot"></span>
    <div>
      <strong id="job-title">正在运行</strong>
      <p id="job-detail"></p>
    </div>
  </div>
</template>

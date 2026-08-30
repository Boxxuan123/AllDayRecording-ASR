<template>
  <section id="view-evaluation" class="view">
    <div class="section-heading">
      <div>
        <p class="eyebrow">GROUND TRUTH</p>
        <h3>人工真值与评测</h3>
        <p>原生 V2 会话使用连续区间真值；旧录音仍可使用逐段听写模板。</p>
      </div>
      <div class="section-actions">
        <label class="select-field compact">
          <span>评测模板</span>
          <select id="evaluation-select" aria-label="选择评测模板"></select>
        </label>
        <button id="run-evaluation-button" class="secondary-button">生成评测报告</button>
      </div>
    </div>

    <div class="review-toolbar">
      <div class="progress-copy">
        <strong id="review-progress-label">0 / 0</strong>
        <span>已填写准确文字</span>
      </div>
      <div class="progress-track" aria-hidden="true">
        <span id="review-progress-bar"></span>
      </div>
      <div class="filter-group" role="group" aria-label="标注筛选">
        <button class="filter-button active" data-filter="all">全部</button>
        <button class="filter-button" data-filter="unlabeled">未标注</button>
        <button class="filter-button" data-filter="labeled">已标注</button>
      </div>
    </div>

    <div class="annotation-purpose">
      <strong>这是一份留出的“考试答案”，不会直接拿去训练模型。</strong>
      <p>
        准确文字测 ASR；真实说话人测多人切分；是否本人测声纹；关键事实测时间、地点和承诺有没有丢失。混合、非语音或听不清时将相关字段留空，必要时取消纳入评测并备注。
      </p>
    </div>

    <div id="session-evaluation-panel" class="session-evaluation-panel hidden"></div>
    <div id="evaluation-empty" class="empty-state hidden">
      <strong>还没有评测模板</strong>
      <p>请先在终端运行 evaluation init，或选择其他录音。</p>
    </div>
    <div id="segment-list" class="segment-list"></div>
    <div id="pagination" class="pagination"></div>
  </section>
</template>

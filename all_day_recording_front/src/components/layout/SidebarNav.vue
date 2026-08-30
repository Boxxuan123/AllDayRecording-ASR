<template>
  <aside class="sidebar" aria-label="主导航">
    <div class="brand-block">
      <div class="brand-mark" aria-hidden="true">AD</div>
      <div>
        <p class="eyebrow">DAYTRACE / LOCAL</p>
        <h1>日记工作台</h1>
      </div>
    </div>

    <nav class="nav-list">
      <button class="nav-item active" data-view="timeline">
        <span class="nav-index">01</span>
        <span>说话人时间轴</span>
      </button>
      <button class="nav-item" data-view="evaluation">
        <span class="nav-index">02</span>
        <span>评测标注</span>
      </button>
      <button class="nav-item" data-view="semantic">
        <span class="nav-index">03</span>
        <span>语义证据</span>
        <span id="semantic-review-badge" class="badge hidden">0</span>
      </button>
      <button class="nav-item" data-view="actions">
        <span class="nav-index">04</span>
        <span>行动候选</span>
        <span id="pending-action-badge" class="badge hidden">0</span>
      </button>
      <button class="nav-item" data-view="runs">
        <span class="nav-index">05</span>
        <span>运行记录</span>
      </button>
    </nav>

    <div class="privacy-card">
      <span class="status-dot"></span>
      <div>
        <strong>仅本机访问</strong>
        <p>录音、真值与声纹不会上传。</p>
      </div>
    </div>
  </aside>
</template>

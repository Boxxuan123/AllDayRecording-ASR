<template>
  <section id="view-actions" class="view">
    <div class="section-heading">
      <div>
        <p class="eyebrow">REVIEW QUEUE</p>
        <h3>日程与待办候选</h3>
        <p>每个候选都有原文、确认语和原音证据；确认后仍不会写入外部日历。</p>
      </div>
    </div>
    <div id="action-list" class="action-list"></div>
    <div id="action-empty" class="empty-state hidden">
      <strong>没有行动候选</strong>
      <p>当前严格规则没有找到同时具备时间、行动和本人承诺的内容。</p>
    </div>
  </section>
</template>

<template>
  <section id="view-runs" class="view">
    <div class="section-heading">
      <div>
        <p class="eyebrow">PROCESS HISTORY</p>
        <h3>当前会话的 V2 运行记录</h3>
        <p>工作流、ASR、说话人和语义阶段都保留配置、状态、摘要与错误信息。</p>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>阶段</th>
            <th>状态</th>
            <th>开始时间</th>
            <th>配置</th>
            <th>结果摘要</th>
          </tr>
        </thead>
        <tbody id="run-table-body"></tbody>
      </table>
    </div>
  </section>
</template>

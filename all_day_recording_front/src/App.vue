<script setup lang="ts">
import { onMounted } from 'vue'
import SidebarNav from './components/layout/SidebarNav.vue'
import WorkspaceHeader from './components/layout/WorkspaceHeader.vue'
import ActionsView from './components/views/ActionsView.vue'
import EvaluationView from './components/views/EvaluationView.vue'
import RunsView from './components/views/RunsView.vue'
import SemanticView from './components/views/SemanticView.vue'
import TimelineView from './components/views/TimelineView.vue'
import { initializeWorkbench } from './workspace/controller.js'

onMounted(() => {
  void initializeWorkbench()
})
</script>

<template>
  <div class="app-shell">
    <SidebarNav />
    <main class="workspace">
      <WorkspaceHeader />
      <TimelineView />
      <SemanticView />
      <EvaluationView />
      <ActionsView />
      <RunsView />
    </main>
  </div>

  <div id="toast-region" class="toast-region" aria-live="polite"></div>
  <datalist id="speaker-labels">
    <option value="self"></option>
    <option value="mother"></option>
    <option value="father"></option>
    <option value="tv"></option>
  </datalist>
</template>

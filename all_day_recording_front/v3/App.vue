<script setup lang="ts">
import AppShell from './components/AppShell.vue'
import { route } from './core/router'
import DataView from './views/DataView.vue'
import DevicesView from './views/DevicesView.vue'
import LabView from './views/LabView.vue'
import OverviewView from './views/OverviewView.vue'
import PeopleView from './views/PeopleView.vue'
import InsightsView from './views/InsightsView.vue'
import ProcessingView from './views/ProcessingView.vue'
import RecordingsView from './views/RecordingsView.vue'
import ReviewsView from './views/ReviewsView.vue'
import RemindersView from './views/RemindersView.vue'
import SessionDetailView from './views/SessionDetailView.vue'
import SettingsView from './views/SettingsView.vue'
</script>

<template>
  <AppShell>
    <OverviewView v-if="route.name === 'overview'" />
    <RecordingsView v-else-if="route.name === 'recordings'" />
    <SessionDetailView v-else-if="route.name === 'session'" :key="route.sessionId ?? ''" />
    <ReviewsView v-else-if="route.name === 'reviews'" />
    <RemindersView v-else-if="route.name === 'reminders'" />
    <PeopleView v-else-if="route.name === 'people'" />
    <InsightsView v-else-if="route.name === 'insights'" />
    <ProcessingView v-else-if="route.name === 'processing'" />
    <DevicesView v-else-if="route.name === 'devices'" />
    <DataView v-else-if="route.name === 'data'" />
    <SettingsView v-else-if="route.name === 'settings'" />
    <LabView v-else />
  </AppShell>
</template>

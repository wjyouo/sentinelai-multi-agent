<template>
  <router-view />
</template>

<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { useAppsStore } from '@/stores/apps'
import { useGraphStore } from '@/stores/graph'
import { useReportStore } from '@/stores/report'
import { useSystemStore } from '@/stores/system'
import { useSSE } from '@/composables/useSSE'
import { usePolling } from '@/composables/usePolling'
import * as appsApi from '@/api/apps'

const appsStore = useAppsStore()
const graphStore = useGraphStore()
const reportStore = useReportStore()
const systemStore = useSystemStore()
const { connect: connectSSE } = useSSE()

// Initial data loads
onMounted(async () => {
  connectSSE()
  await graphStore.checkEnabled()
})

// Poll app status every 5s
const { start: pollAppStatus } = usePolling(async () => {
  try {
    const res = await appsApi.fetchAppStatus()
    if (res.data && res.data.success !== false) {
      Object.entries(res.data).forEach(([name, info]: [string, any]) => {
        if (appsStore.apps[name]) {
          appsStore.apps[name].status = info.status || 'stopped'
          appsStore.apps[name].port = info.port || appsStore.apps[name].port
        }
      })
    }
  } catch { /* ignore */ }
}, 5000)

// Poll system status and report lock status
const { start: pollSystemStatus } = usePolling(async () => {
  await systemStore.fetchStatus()
  if (systemStore.started) {
    await reportStore.fetchStatus()
  } else {
    reportStore.enginesReady = false
  }
}, 5000)

watch(() => appsStore.activeApp, (tab) => {
  if (!appsStore.activeApp) return
  if (tab === 'report') {
    reportStore.fetchStatus()
  }
})

onMounted(() => {
  pollAppStatus()
  pollSystemStatus()
})
</script>

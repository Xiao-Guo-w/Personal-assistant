<script setup lang="ts">
import { computed, watch } from 'vue'
import { RouterView, useRoute } from 'vue-router'

import AppShell from '@/components/AppShell.vue'
import ToastHost from '@/components/ToastHost.vue'
import { authState } from '@/stores/auth'
import { startReminderPolling, stopReminderPolling } from '@/stores/reminders'
import { startSchedulePolling, stopSchedulePolling } from '@/stores/schedules'

const route = useRoute()

/** 登录页 / 引导页不使用侧边栏外壳。 */
const bare = computed(() => Boolean(route.meta.bare))

// 登录且引导完成后才开始轮询：站内提醒（30s）+ 日程（60s）
watch(
  () => authState.user?.onboarded ?? false,
  (onboarded) => {
    if (onboarded) {
      startReminderPolling()
      startSchedulePolling()
    } else {
      stopReminderPolling()
      stopSchedulePolling()
    }
  },
  { immediate: true },
)
</script>

<template>
  <RouterView v-if="bare" />
  <AppShell v-else>
    <RouterView />
  </AppShell>
  <ToastHost />
</template>

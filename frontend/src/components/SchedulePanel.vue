<script setup lang="ts">
import { computed, ref } from 'vue'

import { pushToast } from '@/composables/useToast'
import { cancelSchedule, scheduleState } from '@/stores/schedules'

const expanded = ref(true)

const items = computed(() => scheduleState.upcoming)

async function handleCancel(id: number): Promise<void> {
  const ok = await cancelSchedule(id)
  if (ok) pushToast('已取消日程', 'success', '🗑️')
  else pushToast(scheduleState.error || '取消失败', 'error', '⚠️')
}
</script>

<template>
  <section class="panel">
    <button type="button" class="panel-head" @click="expanded = !expanded">
      <span>📅 日程</span>
      <span class="panel-meta">
        待开始 {{ items.length }}
        <span class="chevron" :class="{ open: expanded }">▾</span>
      </span>
    </button>

    <div v-if="expanded" class="panel-body">
      <p v-if="!items.length" class="muted small">
        暂无日程。对助理说「帮我约张三明天下午 3 点开会」即可创建。
      </p>
      <div v-for="item in items" :key="item.id" class="reminder-row">
        <div class="reminder-text">
          <span class="reminder-icon">📅</span>
          <div>
            <div class="reminder-title">{{ item.title }}</div>
            <div class="muted small">
              {{ item.start_at_local }}
              <template v-if="item.location"> · {{ item.location }}</template>
            </div>
            <div v-if="item.reminder_id" class="muted small">🔔 已设邮件提醒</div>
          </div>
        </div>
        <button type="button" class="btn btn-ghost btn-xs" @click="handleCancel(item.id)">取消</button>
      </div>
    </div>
  </section>
</template>

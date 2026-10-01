<script setup lang="ts">
import { computed, ref } from 'vue'

import { pushToast } from '@/composables/useToast'
import { cancel, reminderState } from '@/stores/reminders'

const expanded = ref(false)

const items = computed(() => reminderState.pending)

async function handleCancel(id: number): Promise<void> {
  const ok = await cancel(id)
  if (ok) pushToast('已取消提醒', 'success', '🗑️')
  else pushToast(reminderState.error || '取消失败', 'error', '⚠️')
}
</script>

<template>
  <section class="panel">
    <button type="button" class="panel-head" @click="expanded = !expanded">
      <span>⏰ 提醒</span>
      <span class="panel-meta">
        待触发 {{ items.length }}
        <span class="chevron" :class="{ open: expanded }">▾</span>
      </span>
    </button>

    <div v-if="expanded" class="panel-body">
      <p v-if="!items.length" class="muted small">暂无待触发的提醒。</p>
      <div v-for="item in items" :key="item.id" class="reminder-row">
        <div class="reminder-text">
          <span class="reminder-icon">{{ item.kind === 'email' ? '📧' : '⏰' }}</span>
          <div>
            <div class="reminder-title">{{ item.text }}</div>
            <div class="muted small">{{ item.remind_at_local }}</div>
          </div>
        </div>
        <button type="button" class="btn btn-ghost btn-xs" @click="handleCancel(item.id)">取消</button>
      </div>
    </div>
  </section>
</template>

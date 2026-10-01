<script setup lang="ts">
import { reminderState, acknowledge } from '@/stores/reminders'

function toneOf(item: { status: string; kind: string }): string {
  if (item.status === 'failed') return 'alert-error'
  if (item.kind === 'email') return 'alert-success'
  return 'alert-warn'
}
</script>

<template>
  <div v-if="reminderState.inbox.length" class="reminder-inbox">
    <div
      v-for="item in reminderState.inbox"
      :key="item.id"
      class="alert"
      :class="toneOf(item)"
    >
      <div class="alert-body">
        <strong v-if="item.status === 'failed'">❌ {{ item.text }}</strong>
        <strong v-else-if="item.kind === 'email'">
          ✅ 定时邮件已发送（{{ item.remind_at_local }}）
        </strong>
        <strong v-else>⏰ 提醒（{{ item.remind_at_local }}）</strong>
        <p v-if="item.kind !== 'email'" class="alert-text">{{ item.text }}</p>
        <p v-if="item.status === 'failed'" class="alert-text">
          原因：{{ item.last_error || '投递失败' }}
        </p>
        <p v-else-if="item.kind === 'email' && item.last_error" class="alert-text">
          {{ item.last_error }}
        </p>
      </div>
      <button type="button" class="btn btn-ghost btn-sm" @click="acknowledge(item.id)">
        知道了
      </button>
    </div>
  </div>
</template>

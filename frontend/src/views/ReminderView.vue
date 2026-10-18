<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { errorMessage, reminderApi } from '@/api'
import type { Reminder } from '@/api/types'
import { pushToast } from '@/composables/useToast'
import { authState } from '@/stores/auth'
import {
  acknowledge,
  loadInbox,
  reminderState,
  refreshReminders,
} from '@/stores/reminders'

const tab = ref<'pending' | 'history'>('pending')
const pending = ref<Reminder[]>([])
const history = ref<Reminder[]>([])
const loading = ref(true)
const error = ref('')
const confirmId = ref<number | null>(null)

const inbox = computed(() => reminderState.inbox)
const failedCount = computed(
  () => history.value.filter((item) => item.status === 'failed').length,
)

onMounted(refreshAll)

function iconOf(item: Reminder): string {
  if (item.kind === 'email') return '📧'
  if (item.schedule_id) return '📅'
  return '⏰'
}

function statusText(item: Reminder): string {
  if (item.status === 'pending') return '待触发'
  if (item.status === 'fired') return '已提醒'
  if (item.status === 'failed') return '发送失败'
  if (item.status === 'cancelled') return '已取消'
  return item.status
}

function statusClass(item: Reminder): string {
  if (item.status === 'fired') return 'success'
  if (item.status === 'failed') return 'failed'
  if (item.status === 'cancelled') return 'muted'
  return 'active'
}

function formatTimestamp(value: string): string {
  if (!value) return ''
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: authState.user?.timezone || 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(new Date(value))
  } catch {
    return value
  }
}

async function refreshAll(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const [pendingData, allData] = await Promise.all([
      reminderApi.list('pending', 100),
      reminderApi.list('all', 100),
      loadInbox(),
    ])
    pending.value = pendingData.reminders
    history.value = allData.reminders.filter((item) => item.status !== 'pending')
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

async function handleCancel(item: Reminder): Promise<void> {
  confirmId.value = null
  try {
    const result = await reminderApi.cancel(item.id)
    if (!result.cancelled) {
      pushToast(result.message || '取消失败', 'error', '⚠️')
      return
    }
    pushToast('已取消提醒', 'success', '🗑️')
    await Promise.all([refreshAll(), refreshReminders()])
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  }
}

async function handleAcknowledge(item: Reminder): Promise<void> {
  await acknowledge(item.id)
  pushToast('已标记为知道了', 'success', '✅')
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>⏰ 提醒</h1>
        <p class="muted small">
          查看待触发提醒和发送记录。日程提醒会同时在应用内和绑定的邮箱中通知你。
        </p>
      </div>
      <button type="button" class="btn btn-ghost btn-sm" :disabled="loading" @click="refreshAll">
        🔄 刷新
      </button>
    </header>

    <div class="reminder-summary">
      <div class="reminder-summary-card">
        <span class="reminder-summary-icon">⏳</span>
        <div>
          <strong>{{ pending.length }}</strong>
          <p class="muted small">待触发</p>
        </div>
      </div>
      <div class="reminder-summary-card">
        <span class="reminder-summary-icon">🔔</span>
        <div>
          <strong>{{ inbox.length }}</strong>
          <p class="muted small">新通知</p>
        </div>
      </div>
      <div class="reminder-summary-card">
        <span class="reminder-summary-icon">⚠️</span>
        <div>
          <strong>{{ failedCount }}</strong>
          <p class="muted small">发送失败</p>
        </div>
      </div>
    </div>

    <p v-if="error" class="alert alert-error">{{ error }}</p>

    <div class="tabs">
      <button
        type="button"
        class="tab"
        :class="{ active: tab === 'pending' }"
        @click="tab = 'pending'"
      >
        待提醒（{{ pending.length }}）
      </button>
      <button
        type="button"
        class="tab"
        :class="{ active: tab === 'history' }"
        @click="tab = 'history'"
      >
        历史记录（{{ history.length }}）
      </button>
    </div>

    <p v-if="loading" class="muted">加载中…</p>

    <template v-else-if="tab === 'pending'">
      <section v-if="inbox.length" class="stack">
        <h2 class="group-title">刚刚到点</h2>
        <article
          v-for="item in inbox"
          :key="`inbox-${item.id}`"
          class="card reminder-card inbox-card"
        >
          <span class="reminder-card-icon">{{ iconOf(item) }}</span>
          <div class="reminder-card-main">
            <div class="reminder-card-title">
              <h3>{{ item.text }}</h3>
              <span class="status-pill" :class="item.status === 'failed' ? 'failed' : 'success'">
                {{ item.status === 'failed' ? '发送失败' : '已到点' }}
              </span>
            </div>
            <p class="muted small">{{ item.remind_at_local }}</p>
            <p v-if="item.last_error" class="reminder-error">{{ item.last_error }}</p>
          </div>
          <button type="button" class="btn btn-ghost btn-xs" @click="handleAcknowledge(item)">
            知道了
          </button>
        </article>
      </section>

      <section class="stack">
        <h2 class="group-title">待触发提醒</h2>
        <div v-if="!pending.length" class="empty-state">
          <p>暂无待触发的提醒。</p>
          <p class="muted small">对助手说「提醒我今晚 8 点交周报」即可创建。</p>
        </div>

        <article v-for="item in pending" :key="item.id" class="card reminder-card">
          <span class="reminder-card-icon">{{ iconOf(item) }}</span>
          <div class="reminder-card-main">
            <div class="reminder-card-title">
              <h3>{{ item.text }}</h3>
              <span class="status-pill active">待触发</span>
            </div>
            <p class="muted small">{{ item.remind_at_local }}</p>
            <p v-if="item.kind === 'email' && item.to" class="muted small">
              收件人：{{ item.to }}
            </p>
          </div>

          <div v-if="confirmId !== item.id" class="reminder-actions">
            <button type="button" class="btn btn-ghost btn-xs" @click="confirmId = item.id">
              取消
            </button>
          </div>
          <div v-else class="confirm-box reminder-confirm">
            <p class="small">确定取消这条提醒？</p>
            <div class="confirm-actions">
              <button type="button" class="btn btn-danger btn-xs" @click="handleCancel(item)">
                确认取消
              </button>
              <button type="button" class="btn btn-ghost btn-xs" @click="confirmId = null">
                返回
              </button>
            </div>
          </div>
        </article>
      </section>
    </template>

    <template v-else>
      <div v-if="!history.length" class="empty-state">
        <p>还没有历史记录。</p>
        <p class="muted small">提醒触发或取消后会出现在这里。</p>
      </div>

      <article v-for="item in history" :key="item.id" class="card reminder-card">
        <span class="reminder-card-icon">{{ iconOf(item) }}</span>
        <div class="reminder-card-main">
          <div class="reminder-card-title">
            <h3>{{ item.text }}</h3>
            <span class="status-pill" :class="statusClass(item)">{{ statusText(item) }}</span>
          </div>
          <p class="muted small">计划时间：{{ item.remind_at_local }}</p>
          <p v-if="item.fired_at" class="muted small">
            触发时间：{{ formatTimestamp(item.fired_at) }}
          </p>
          <p v-if="item.fired_via" class="muted small">通知渠道：{{ item.fired_via }}</p>
          <p v-if="item.last_error" class="reminder-error">{{ item.last_error }}</p>
        </div>
      </article>
    </template>
  </div>
</template>

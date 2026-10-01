import { computed, reactive } from 'vue'

import { errorMessage, reminderApi } from '@/api'
import type { Reminder } from '@/api/types'

interface ReminderState {
  /** 已触发但用户还没点「知道了」的站内通知 */
  inbox: Reminder[]
  /** 待触发列表（侧边栏管理用） */
  pending: Reminder[]
  loading: boolean
  error: string
}

export const reminderState = reactive<ReminderState>({
  inbox: [],
  pending: [],
  loading: false,
  error: '',
})

export const unreadCount = computed(() => reminderState.inbox.length)

let poller: number | null = null

async function silent<T>(task: () => Promise<T>): Promise<T | null> {
  try {
    return await task()
  } catch {
    // 轮询失败不打扰用户（后端重启时很常见）
    return null
  }
}

export async function loadInbox(): Promise<void> {
  const data = await silent(() => reminderApi.inbox())
  if (data) reminderState.inbox = data.reminders
}

export async function loadPending(): Promise<void> {
  const data = await silent(() => reminderApi.list('pending', 10))
  if (data) reminderState.pending = data.reminders
}

export async function refreshReminders(): Promise<void> {
  reminderState.loading = true
  await Promise.all([loadInbox(), loadPending()])
  reminderState.loading = false
}

export async function acknowledge(id: number): Promise<void> {
  reminderState.inbox = reminderState.inbox.filter((item) => item.id !== id)
  try {
    await reminderApi.ack(id)
  } catch (error) {
    reminderState.error = errorMessage(error)
    await loadInbox()
  }
}

export async function cancel(id: number): Promise<boolean> {
  try {
    const result = await reminderApi.cancel(id)
    if (!result.cancelled) {
      reminderState.error = result.message || '取消失败'
      return false
    }
    await loadPending()
    return true
  } catch (error) {
    reminderState.error = errorMessage(error)
    return false
  }
}

/** 每 30 秒问一次后端，到点的提醒会自动冒出来。 */
export function startReminderPolling(intervalMs = 30000): void {
  stopReminderPolling()
  void refreshReminders()
  poller = window.setInterval(() => {
    void loadInbox()
    void loadPending()
  }, intervalMs)
}

export function stopReminderPolling(): void {
  if (poller !== null) {
    window.clearInterval(poller)
    poller = null
  }
}

export function resetReminders(): void {
  stopReminderPolling()
  reminderState.inbox = []
  reminderState.pending = []
  reminderState.error = ''
}

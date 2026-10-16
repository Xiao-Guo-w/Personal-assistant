import { reactive } from 'vue'

import { errorMessage, scheduleApi } from '@/api'
import type { Schedule } from '@/api/types'
import { refreshReminders } from '@/stores/reminders'

interface ScheduleState {
  /** 从现在起的有效日程（侧边栏管理用） */
  upcoming: Schedule[]
  loading: boolean
  error: string
}

export const scheduleState = reactive<ScheduleState>({
  upcoming: [],
  loading: false,
  error: '',
})

let poller: number | null = null

async function silent<T>(task: () => Promise<T>): Promise<T | null> {
  try {
    return await task()
  } catch {
    // 轮询失败不打扰用户（后端重启时很常见）
    return null
  }
}

export async function loadSchedules(): Promise<void> {
  const data = await silent(() => scheduleApi.list('active', 20))
  if (data) scheduleState.upcoming = data.schedules
}

export async function refreshSchedules(): Promise<void> {
  scheduleState.loading = true
  await loadSchedules()
  scheduleState.loading = false
}

export async function cancelSchedule(id: number): Promise<boolean> {
  try {
    const result = await scheduleApi.cancel(id)
    if (!result.cancelled) {
      scheduleState.error = result.message || '取消失败'
      return false
    }
    // 取消日程会把它还没发出的提醒一并取消，两边都要刷新
    await Promise.all([loadSchedules(), refreshReminders()])
    return true
  } catch (error) {
    scheduleState.error = errorMessage(error)
    return false
  }
}

/** 每 60 秒问一次后端：在聊天里新建的日程会自动出现在面板里。 */
export function startSchedulePolling(intervalMs = 60000): void {
  stopSchedulePolling()
  void refreshSchedules()
  poller = window.setInterval(() => {
    void loadSchedules()
  }, intervalMs)
}

export function stopSchedulePolling(): void {
  if (poller !== null) {
    window.clearInterval(poller)
    poller = null
  }
}

export function resetSchedules(): void {
  stopSchedulePolling()
  scheduleState.upcoming = []
  scheduleState.error = ''
}

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { errorMessage, scheduleApi } from '@/api'
import type { Schedule } from '@/api/types'
import { pushToast } from '@/composables/useToast'
import { authState } from '@/stores/auth'
import { cancelSchedule, scheduleState } from '@/stores/schedules'

interface CalendarCell {
  date: Date
  key: string
  day: number
  inMonth: boolean
  isToday: boolean
}

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

const timezone = computed(() => authState.user?.timezone || 'Asia/Shanghai')
const todayKey = currentDateKey(timezone.value)
const initial = keyParts(todayKey)

const cursor = ref(new Date(initial.year, initial.month - 1, 1))
const selectedKey = ref(todayKey)
const schedules = ref<Schedule[]>([])
const loading = ref(false)
const error = ref('')
const confirmId = ref<number | null>(null)

const monthLabel = computed(
  () => `${cursor.value.getFullYear()} 年 ${cursor.value.getMonth() + 1} 月`,
)

const cells = computed<CalendarCell[]>(() => {
  const year = cursor.value.getFullYear()
  const month = cursor.value.getMonth()
  const first = new Date(year, month, 1)
  const offset = (first.getDay() + 6) % 7
  const start = new Date(year, month, 1 - offset)

  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index)
    const key = dateKey(date)
    return {
      date,
      key,
      day: date.getDate(),
      inMonth: date.getMonth() === month,
      isToday: key === todayKey,
    }
  })
})

const eventsByDay = computed(() => {
  const map = new Map<string, Schedule[]>()
  for (const item of schedules.value) {
    const key = item.start_at_local.slice(0, 10)
    const bucket = map.get(key) ?? []
    bucket.push(item)
    map.set(key, bucket)
  }
  return map
})

const selectedSchedules = computed(() => eventsByDay.value.get(selectedKey.value) ?? [])
const selectedDateLabel = computed(() => {
  const parts = keyParts(selectedKey.value)
  const date = new Date(parts.year, parts.month - 1, parts.day)
  return `${parts.month} 月 ${parts.day} 日 · 星期${WEEKDAYS[(date.getDay() + 6) % 7]}`
})

onMounted(loadMonth)
watch(cursor, loadMonth)

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function dateKey(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function keyParts(key: string): { year: number; month: number; day: number } {
  const [year, month, day] = key.split('-').map(Number)
  return { year, month, day }
}

function currentDateKey(tz: string): string {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(new Date())
    const get = (type: string) => parts.find((part) => part.type === type)?.value ?? ''
    return `${get('year')}-${get('month')}-${get('day')}`
  } catch {
    const now = new Date()
    return dateKey(now)
  }
}

function gridBounds(): { from: string; to: string } {
  const list = cells.value
  return {
    from: `${list[0].key}T00:00:00`,
    to: `${list[list.length - 1].key}T23:59:59`,
  }
}

async function loadMonth(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const bounds = gridBounds()
    const data = await scheduleApi.list('active', 1000, bounds.from, bounds.to)
    // 后端上限是 1000，这里不再按返回顺序假设；按开始时间稳定排序
    schedules.value = [...data.schedules].sort((a, b) =>
      a.start_at.localeCompare(b.start_at),
    )
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

function goMonth(delta: number): void {
  const next = new Date(cursor.value.getFullYear(), cursor.value.getMonth() + delta, 1)
  cursor.value = next
  selectedKey.value = dateKey(next)
  confirmId.value = null
}

function goToday(): void {
  const parts = keyParts(todayKey)
  cursor.value = new Date(parts.year, parts.month - 1, 1)
  selectedKey.value = todayKey
  confirmId.value = null
}

function selectCell(cell: CalendarCell): void {
  selectedKey.value = cell.key
  confirmId.value = null
  if (!cell.inMonth) {
    cursor.value = new Date(cell.date.getFullYear(), cell.date.getMonth(), 1)
  }
}

function timeRange(item: Schedule): string {
  return `${item.start_at_local.slice(11, 16)}–${item.end_at_local.slice(11, 16)}`
}

async function handleCancel(item: Schedule): Promise<void> {
  confirmId.value = null
  const ok = await cancelSchedule(item.id)
  if (ok) {
    pushToast(`已取消：${item.title}`, 'success', '🗑️')
    await loadMonth()
  } else {
    pushToast(scheduleState.error || '取消失败', 'error', '⚠️')
  }
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>📅 日程</h1>
        <p class="muted small">
          按月查看全部日程。新建、改期或取消日程，可以直接在聊天里告诉助手。
        </p>
      </div>
      <button type="button" class="btn btn-ghost btn-sm" :disabled="loading" @click="loadMonth">
        🔄 刷新
      </button>
    </header>

    <p v-if="error" class="alert alert-error">{{ error }}</p>

    <div class="calendar-layout">
      <section class="card calendar-card">
        <div class="calendar-toolbar">
          <div class="calendar-month">
            <h2>{{ monthLabel }}</h2>
            <span v-if="loading" class="muted small">加载中…</span>
          </div>
          <div class="row">
            <button type="button" class="icon-btn calendar-nav" title="上个月" @click="goMonth(-1)">
              ‹
            </button>
            <button type="button" class="btn btn-ghost btn-sm" @click="goToday">今天</button>
            <button type="button" class="icon-btn calendar-nav" title="下个月" @click="goMonth(1)">
              ›
            </button>
          </div>
        </div>

        <div class="calendar-weekdays">
          <span v-for="weekday in WEEKDAYS" :key="weekday">{{ weekday }}</span>
        </div>

        <div class="calendar-grid">
          <button
            v-for="cell in cells"
            :key="cell.key"
            type="button"
            class="calendar-day"
            :class="{
              muted: !cell.inMonth,
              today: cell.isToday,
              selected: selectedKey === cell.key,
            }"
            @click="selectCell(cell)"
          >
            <span class="calendar-day-head">
              <span class="calendar-number">{{ cell.day }}</span>
              <span v-if="cell.isToday" class="calendar-today-label">今天</span>
            </span>

            <span class="calendar-events">
              <span
                v-for="item in (eventsByDay.get(cell.key) ?? []).slice(0, 2)"
                :key="item.id"
                class="calendar-event"
              >
                <span class="calendar-event-time">{{ item.start_at_local.slice(11, 16) }}</span>
                <span class="calendar-event-title">{{ item.title }}</span>
              </span>
              <span
                v-if="(eventsByDay.get(cell.key) ?? []).length > 2"
                class="calendar-more"
              >
                +{{ (eventsByDay.get(cell.key) ?? []).length - 2 }} 项
              </span>
            </span>
          </button>
        </div>
      </section>

      <aside class="card calendar-agenda">
        <div class="agenda-head">
          <div>
            <h2>{{ selectedDateLabel }}</h2>
            <p class="muted small">
              {{ selectedSchedules.length ? `共 ${selectedSchedules.length} 项安排` : '这一天还没有安排' }}
            </p>
          </div>
          <span class="agenda-date-badge">{{ keyParts(selectedKey).day }}</span>
        </div>

        <div v-if="!selectedSchedules.length" class="agenda-empty">
          <span>☀️</span>
          <p class="muted small">留白也是一种安排。</p>
        </div>

        <article v-for="item in selectedSchedules" :key="item.id" class="agenda-item">
          <div class="agenda-time">{{ timeRange(item) }}</div>
          <div class="agenda-body">
            <h3>{{ item.title }}</h3>
            <p v-if="item.location" class="muted small">📍 {{ item.location }}</p>
            <p v-if="item.description" class="muted small agenda-note">
              {{ item.description }}
            </p>
            <p v-if="item.reminder_id" class="muted small">🔔 已设置提醒</p>

            <button
              v-if="confirmId !== item.id"
              type="button"
              class="btn btn-ghost btn-xs agenda-cancel"
              @click="confirmId = item.id"
            >
              取消日程
            </button>
            <div v-else class="confirm-box">
              <p class="small">取消后，关联的待发提醒也会一并取消。</p>
              <div class="confirm-actions">
                <button type="button" class="btn btn-danger btn-xs" @click="handleCancel(item)">
                  确认取消
                </button>
                <button type="button" class="btn btn-ghost btn-xs" @click="confirmId = null">
                  返回
                </button>
              </div>
            </div>
          </div>
        </article>
      </aside>
    </div>
  </div>
</template>

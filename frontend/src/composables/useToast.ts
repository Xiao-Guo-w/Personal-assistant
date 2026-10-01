import { reactive } from 'vue'

export type ToastTone = 'info' | 'success' | 'error'

export interface ToastItem {
  id: number
  text: string
  tone: ToastTone
  icon: string
}

export const toasts = reactive<ToastItem[]>([])

let nextId = 1

export function pushToast(text: string, tone: ToastTone = 'info', icon = ''): void {
  const id = nextId++
  toasts.push({ id, text, tone, icon })
  window.setTimeout(() => dismissToast(id), 3200)
}

export function dismissToast(id: number): void {
  const index = toasts.findIndex((item) => item.id === id)
  if (index >= 0) toasts.splice(index, 1)
}

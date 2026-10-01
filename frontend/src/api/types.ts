/** 与 backend/main.py 的响应模型一一对应。 */

export interface User {
  id: number
  username: string
  display_name: string
  timezone: string
  onboarded: boolean
}

export interface AuthResponse {
  token: string
  user: User
}

export interface SessionMeta {
  id: string
  title: string
  created_at: string | null
  updated_at: string | null
}

export type ChatStatus = 'IDLE' | 'DONE' | 'WAITING_CONFIRMATION'

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  /** 前端本地标记：请求失败的消息用红色气泡展示 */
  failed?: boolean
}

export interface PendingAction {
  tool?: string
  args?: Record<string, unknown>
  description?: string
  [key: string]: unknown
}

export interface ChatResponse {
  session_id: string
  reply: string
  status: ChatStatus
  pending_action: PendingAction | null
}

export interface SessionDetail {
  id: string
  stage: string | null
  pending_action: PendingAction | null
  messages: { role: string; content: string }[]
}

export interface MemoryItem {
  key: string
  label: string
  category: string
  icon: string
  raw_value: string
  display_value: string
  updated_at: string | null
}

export interface FeishuStatus {
  connected: boolean
  name?: string
  open_id?: string
}

export interface NotionStatus {
  connected: boolean
  workspace_name?: string
  workspace_id?: string
  default_parent_page_id?: string
}

export interface EmailStatus {
  connected: boolean
  address?: string
}

export interface IntegrationStatus {
  feishu_calendar: FeishuStatus
  notion: NotionStatus
  email: EmailStatus
}

export interface NotionPage {
  id: string
  title: string
  is_top_level?: boolean
  url?: string
}

export interface EmailConfig {
  address: string
  auth_code_masked: string
  configured: boolean
}

export type ReminderKind = 'reminder' | 'email'
export type ReminderStatus = 'pending' | 'firing' | 'fired' | 'failed' | 'cancelled'

export interface Reminder {
  id: number
  reminder_id: string
  kind: ReminderKind
  text: string
  to: string
  subject: string
  remind_at: string
  remind_at_local: string
  status: ReminderStatus
  fired_via: string
  last_error: string
  created_at: string | null
  fired_at: string | null
}

export interface ReminderListResponse {
  count: number
  status?: string
  reminders: Reminder[]
}

export interface ReminderInboxResponse {
  count: number
  reminders: Reminder[]
}

export interface CancelReminderResponse {
  cancelled: boolean
  message?: string
}

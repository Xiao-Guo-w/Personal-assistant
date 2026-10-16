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

/** 流式对话的回调集合（见 sessionApi.chatStream）。 */
export interface ChatStreamHandlers {
  /** 增量文本：追加到当前气泡 */
  onToken?: (text: string) => void
  /** 整条替换：平台生成的文案，或防幻觉校正后的最终文本 */
  onReplace?: (text: string) => void
  /** 进度提示：等待期间的状态行（如"正在检索知识库…"），有正文后即失效 */
  onStep?: (text: string) => void
  /** 本轮结束：带上确认状态，供前端决定是否弹确认栏 */
  onDone?: (payload: { status: ChatStatus; pendingAction: PendingAction | null }) => void
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

export interface EmailStatus {
  connected: boolean
  address?: string
}

/** 集成状态：只保留用户绑定的 QQ 邮箱（日程与提醒都在系统内部）。 */
export interface IntegrationStatus {
  email: EmailStatus
}

export interface EmailConfig {
  address: string
  auth_code_masked: string
  configured: boolean
}

/* ---------- 日程 ---------- */

export type ScheduleStatus = 'active' | 'cancelled'

export interface Schedule {
  id: number
  schedule_id: string
  title: string
  start_at: string
  start_at_local: string
  end_at: string
  end_at_local: string
  location: string
  description: string
  status: ScheduleStatus
  /** 关联的提醒 ID（形如 rem_12）；空串表示这条日程没有提醒 */
  reminder_id: string
  created_at: string | null
}

export interface ScheduleListResponse {
  count: number
  status?: string
  from_local?: string
  to_local?: string
  schedules: Schedule[]
}

export interface CancelScheduleResponse {
  cancelled: boolean
  message?: string
  title?: string
  start_at_local?: string
  cancelled_reminders?: number
}

export type ReminderKind = 'reminder' | 'email'
export type ReminderStatus = 'pending' | 'firing' | 'fired' | 'failed' | 'cancelled'

export interface Reminder {
  id: number
  reminder_id: string
  kind: ReminderKind
  /** 关联的日程 ID；0 表示这是普通提醒 */
  schedule_id: number
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

/* ---------- 知识库 ---------- */

/** 入库状态。前端据此决定文案与可用操作（进行中不可删）。 */
export type KbDocStatus = 'pending' | 'indexing' | 'ready' | 'failed'

export interface KbDocument {
  id: number
  title: string
  /** 扩展名（不含点），如 pdf / docx / md / txt，用于分组 */
  ext: string
  mime: string
  size_bytes: number
  char_count: number
  chunk_count: number
  status: KbDocStatus
  error: string
  embedding_model: string
  created_at: string | null
  updated_at: string | null
}

export interface KbChunk {
  id: number
  idx: number
  text: string
  token_count: number
  has_vector: boolean
}

export interface KbStats {
  documents: number
  chunks: number
  chars: number
  vectors: number
  vector_configured: boolean
  embedding_model: string
  embedding_dim: number
}

export interface KbHit {
  doc_id: number
  source: string
  chunk_id: number
  score: number
  /** 原始余弦相似度；null 表示这条只被关键词通道命中 */
  similarity: number | null
  tokens: number
  text: string
}

export interface KbSearchResponse {
  query: string
  count: number
  hits: KbHit[]
}

export interface KbRebuildResponse {
  chunks: number
  vectors: number
  reembedded: number
}

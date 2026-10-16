/** 后端接口的薄封装，按业务域分组，视图层只调用这里的函数。 */

import { ApiError, http, isNotFound, streamPost } from './http'
import type {
  AuthResponse,
  CancelScheduleResponse,
  CancelReminderResponse,
  ChatStatus,
  ChatStreamHandlers,
  EmailConfig,
  IntegrationStatus,
  KbChunk,
  KbDocument,
  KbRebuildResponse,
  KbSearchResponse,
  KbStats,
  MemoryItem,
  PendingAction,
  ReminderInboxResponse,
  ReminderListResponse,
  ScheduleListResponse,
  SessionDetail,
  SessionMeta,
  User,
} from './types'

export const authApi = {
  login: (username: string, password: string) =>
    http.post<AuthResponse>('/api/auth/login', { json: { username, password } }),

  register: (payload: { username: string; password: string; display_name: string; timezone: string }) =>
    http.post<AuthResponse>('/api/auth/register', { json: payload }),

  logout: () => http.post<{ ok: boolean }>('/api/auth/logout', { timeoutMs: 8000 }),

  me: () => http.get<User>('/api/me'),

  updateProfile: (payload: { display_name?: string; timezone?: string }) =>
    http.patch<User>('/api/me', { json: payload }),

  onboardingStatus: () => http.get<{ onboarded: boolean }>('/api/onboarding/status'),

  completeOnboarding: (payload: { timezone?: string; default_meeting_duration?: number }) =>
    http.post<{ ok: boolean }>('/api/onboarding/complete', { json: payload }),
}

export const sessionApi = {
  list: () => http.get<SessionMeta[]>('/api/sessions'),
  create: () => http.post<SessionMeta>('/api/sessions'),
  remove: (id: string) => http.delete<{ ok: boolean }>(`/api/sessions/${id}`),
  detail: (id: string) => http.get<SessionDetail>(`/api/sessions/${id}`),

  /**
   * 流式对话（SSE）。函数返回时本轮已结束。
   *
   * 事件语义见后端 /api/chat/stream：step 进度提示、token 追加、
   * replace 整条替换、done 收尾。
   * 服务端的 error 事件在这里转成 ApiError 抛出，调用方按普通请求失败处理。
   */
  chatStream: (sessionId: string, message: string, handlers: ChatStreamHandlers) =>
    streamPost(
      '/api/chat/stream',
      { session_id: sessionId, message },
      (event) => {
        const data = (event.data ?? {}) as Record<string, unknown>
        if (event.event === 'step') {
          handlers.onStep?.(String(data.text ?? ''))
        } else if (event.event === 'token') {
          handlers.onToken?.(String(data.text ?? ''))
        } else if (event.event === 'replace') {
          handlers.onReplace?.(String(data.text ?? ''))
        } else if (event.event === 'done') {
          handlers.onDone?.({
            status: (data.status as ChatStatus) ?? 'DONE',
            pendingAction: (data.pending_action as PendingAction | null) ?? null,
          })
        } else if (event.event === 'error') {
          throw new ApiError(0, String(data.message ?? '对话失败，请重试。'))
        }
      },
    ),
}

export const memoryApi = {
  list: () => http.get<MemoryItem[]>('/api/memory'),
  remove: (key: string) => http.delete<{ ok: boolean }>(`/api/memory/${encodeURIComponent(key)}`),
}

export const integrationApi = {
  status: () => http.get<IntegrationStatus>('/api/integrations/status'),
  disconnect: (provider: string) => http.delete<{ ok: boolean }>(`/api/integrations/${provider}`),

  /** 未配置时后端返回 404，这里统一转成 null。 */
  async emailConfig(): Promise<EmailConfig | null> {
    try {
      return await http.get<EmailConfig>('/api/integrations/email')
    } catch (error) {
      if (isNotFound(error)) return null
      throw error
    }
  },

  saveEmail: (address: string, authCode: string) =>
    http.put<{ ok: boolean }>('/api/integrations/email', { json: { address, auth_code: authCode } }),

  deleteEmail: () => http.delete<{ ok: boolean }>('/api/integrations/email'),
}

export const scheduleApi = {
  /** 默认从现在起的有效日程；status 取 active / cancelled / all。 */
  list: (status = 'active', limit = 20, timeMin = '') =>
    http.get<ScheduleListResponse>('/api/schedules', {
      params: { status, limit, time_min: timeMin },
    }),
  cancel: (id: number) => http.post<CancelScheduleResponse>(`/api/schedules/${id}/cancel`),
}

export const reminderApi = {
  list: (status = 'pending', limit = 10) =>
    http.get<ReminderListResponse>('/api/reminders', { params: { status, limit } }),
  inbox: () => http.get<ReminderInboxResponse>('/api/reminders/inbox'),
  ack: (id: number) => http.post<{ ok: boolean }>(`/api/reminders/${id}/ack`),
  cancel: (id: number) => http.post<CancelReminderResponse>(`/api/reminders/${id}/cancel`),
}

export const kbApi = {
  list: () => http.get<KbDocument[]>('/api/kb/documents'),

  /** 上传并解析。解析在后端同步完成，慢的向量化交给后台 worker。 */
  upload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    // 解析大文件可能需要几十秒，超时给足
    return http.post<KbDocument>('/api/kb/documents', { formData: form, timeoutMs: 120000 })
  },

  remove: (id: number) => http.delete<{ ok: boolean }>(`/api/kb/documents/${id}`),

  reindex: (id: number) => http.post<{ ok: boolean }>(`/api/kb/documents/${id}/reindex`),

  chunks: (id: number, limit = 50) =>
    http.get<KbChunk[]>(`/api/kb/documents/${id}/chunks`, { params: { limit } }),

  search: (query: string, topK?: number) =>
    http.post<KbSearchResponse>('/api/kb/search', { json: { query, top_k: topK } }),

  stats: () => http.get<KbStats>('/api/kb/stats'),

  rebuild: () => http.post<KbRebuildResponse>('/api/kb/rebuild', { timeoutMs: 300000 }),
}

export { ApiError, errorMessage, getToken, isNotFound, onUnauthorized, setToken } from './http'

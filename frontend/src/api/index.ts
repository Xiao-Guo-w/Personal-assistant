/** 后端接口的薄封装，按业务域分组，视图层只调用这里的函数。 */

import { http, isNotFound } from './http'
import type {
  AuthResponse,
  CancelReminderResponse,
  ChatResponse,
  EmailConfig,
  IntegrationStatus,
  MemoryItem,
  NotionPage,
  ReminderInboxResponse,
  ReminderListResponse,
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
  chat: (sessionId: string, message: string) =>
    http.post<ChatResponse>('/api/chat', {
      json: { session_id: sessionId, message },
      // Agent 可能连续跑多轮工具调用，给足超时时间
      timeoutMs: 180000,
    }),
}

export const memoryApi = {
  list: () => http.get<MemoryItem[]>('/api/memory'),
  remove: (key: string) => http.delete<{ ok: boolean }>(`/api/memory/${encodeURIComponent(key)}`),
}

export const integrationApi = {
  status: () => http.get<IntegrationStatus>('/api/integrations/status'),
  disconnect: (provider: string) => http.delete<{ ok: boolean }>(`/api/integrations/${provider}`),

  authorizeUrl: (provider: 'feishu' | 'notion') =>
    http.get<{ authorize_url: string; state: string }>(`/api/oauth/${provider}/authorize`),

  notionPages: () =>
    http.get<{ pages: NotionPage[] }>('/api/integrations/notion/pages', { timeoutMs: 30000 }),

  setNotionParentPage: (pageId: string) =>
    http.put<{ ok: boolean; page_id: string; title: string }>(
      '/api/integrations/notion/default-parent-page',
      { params: { page_id: pageId }, timeoutMs: 30000 },
    ),

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

export const reminderApi = {
  list: (status = 'pending', limit = 10) =>
    http.get<ReminderListResponse>('/api/reminders', { params: { status, limit } }),
  inbox: () => http.get<ReminderInboxResponse>('/api/reminders/inbox'),
  ack: (id: number) => http.post<{ ok: boolean }>(`/api/reminders/${id}/ack`),
  cancel: (id: number) => http.post<CancelReminderResponse>(`/api/reminders/${id}/cancel`),
}

export { ApiError, errorMessage, getToken, isNotFound, onUnauthorized, setToken } from './http'

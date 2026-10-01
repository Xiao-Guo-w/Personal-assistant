import { computed, reactive } from 'vue'

import { errorMessage, isNotFound, sessionApi } from '@/api'
import type { ChatMessage, ChatStatus, SessionMeta } from '@/api/types'

interface ChatState {
  sessions: SessionMeta[]
  currentSessionId: string | null
  messages: ChatMessage[]
  status: ChatStatus
  /** 正在等待后端回复（Agent 思考中） */
  sending: boolean
  loadingSessions: boolean
  loadingMessages: boolean
  error: string
  /** 等待二次确认删除的会话 id */
  pendingDeleteId: string | null
}

export const chatState = reactive<ChatState>({
  sessions: [],
  currentSessionId: null,
  messages: [],
  status: 'IDLE',
  sending: false,
  loadingSessions: false,
  loadingMessages: false,
  error: '',
  pendingDeleteId: null,
})

export const currentSession = computed(
  () => chatState.sessions.find((item) => item.id === chatState.currentSessionId) ?? null,
)

export async function loadSessions(): Promise<void> {
  chatState.loadingSessions = true
  try {
    chatState.sessions = await sessionApi.list()
  } catch (error) {
    chatState.error = errorMessage(error)
  } finally {
    chatState.loadingSessions = false
  }
}

export async function createSession(): Promise<string | null> {
  try {
    const session = await sessionApi.create()
    chatState.currentSessionId = session.id
    chatState.messages = []
    chatState.status = 'IDLE'
    chatState.error = ''
    await loadSessions()
    return session.id
  } catch (error) {
    chatState.error = errorMessage(error)
    return null
  }
}

export async function openSession(sessionId: string): Promise<void> {
  chatState.currentSessionId = sessionId
  chatState.loadingMessages = true
  chatState.error = ''
  try {
    const detail = await sessionApi.detail(sessionId)
    chatState.messages = detail.messages
      .filter((message) => message.role === 'user' || message.role === 'assistant')
      .map((message) => ({ role: message.role as ChatMessage['role'], content: message.content }))
    chatState.status = detail.stage === 'WAITING_CONFIRMATION' ? 'WAITING_CONFIRMATION' : 'IDLE'
  } catch (error) {
    if (isNotFound(error)) {
      chatState.messages = []
      chatState.status = 'IDLE'
    } else {
      chatState.error = errorMessage(error)
    }
  } finally {
    chatState.loadingMessages = false
  }
}

export async function deleteSession(sessionId: string): Promise<boolean> {
  try {
    await sessionApi.remove(sessionId)
  } catch (error) {
    chatState.error = errorMessage(error)
    return false
  }

  if (chatState.currentSessionId === sessionId) {
    chatState.currentSessionId = null
    chatState.messages = []
    chatState.status = 'IDLE'
  }
  chatState.pendingDeleteId = null
  await loadSessions()
  return true
}

/** 发送一条消息；返回本次是否成功。 */
export async function sendMessage(text: string): Promise<boolean> {
  const content = text.trim()
  if (!content || chatState.sending) return false

  if (!chatState.currentSessionId) {
    const created = await createSession()
    if (!created) return false
  }

  const sessionId = chatState.currentSessionId as string
  chatState.messages.push({ role: 'user', content })
  chatState.sending = true
  chatState.error = ''

  try {
    const data = await sessionApi.chat(sessionId, content)
    chatState.messages.push({ role: 'assistant', content: data.reply || '(Agent 没有返回内容)' })
    chatState.status = data.status === 'WAITING_CONFIRMATION' ? 'WAITING_CONFIRMATION' : 'DONE'
    // 首条消息会改写会话标题，同步一下列表
    await loadSessions()
    return true
  } catch (error) {
    chatState.messages.push({ role: 'assistant', content: errorMessage(error), failed: true })
    chatState.status = 'IDLE'
    return false
  } finally {
    chatState.sending = false
  }
}

export function resetChat(): void {
  chatState.sessions = []
  chatState.currentSessionId = null
  chatState.messages = []
  chatState.status = 'IDLE'
  chatState.sending = false
  chatState.error = ''
  chatState.pendingDeleteId = null
}

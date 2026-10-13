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
  /**
   * 流式回答的进行中内容。
   * null 表示当前没有流；空串表示已开流但还没有 token（此时展示打字动画）。
   */
  streamingContent: string | null
  /**
   * 等待期间的进度提示（"正在检索知识库…"）。
   * 首个 token 到达后置空，避免和正文同时显示。
   */
  streamingStep: string | null
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
  streamingContent: null,
  streamingStep: null,
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
    chatState.streamingContent = null
    chatState.streamingStep = null
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
  chatState.streamingContent = null
  chatState.streamingStep = null
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
  chatState.streamingContent = ''
  chatState.streamingStep = null
  chatState.error = ''

  try {
    await sessionApi.chatStream(sessionId, content, {
      onStep: (text) => {
        chatState.streamingStep = text
      },
      onToken: (delta) => {
        chatState.streamingContent = (chatState.streamingContent ?? '') + delta
        // 正文开始输出了，状态行让位
        chatState.streamingStep = null
      },
      onReplace: (full) => {
        chatState.streamingContent = full
      },
      onDone: ({ status }) => {
        chatState.status = status === 'WAITING_CONFIRMATION' ? 'WAITING_CONFIRMATION' : 'DONE'
      },
    })

    // 落定成正式消息：先清流式占位再 push，避免同一次渲染里出现两个气泡
    const reply = chatState.streamingContent || '(Agent 没有返回内容)'
    chatState.streamingContent = null
    chatState.messages.push({ role: 'assistant', content: reply })
    // 首条消息会改写会话标题，同步一下列表
    await loadSessions()
    return true
  } catch (error) {
    chatState.messages.push({ role: 'assistant', content: errorMessage(error), failed: true })
    chatState.status = 'IDLE'
    return false
  } finally {
    chatState.streamingContent = null
    chatState.streamingStep = null
    chatState.sending = false
  }
}

export function resetChat(): void {
  chatState.sessions = []
  chatState.currentSessionId = null
  chatState.messages = []
  chatState.status = 'IDLE'
  chatState.sending = false
  chatState.streamingContent = null
  chatState.streamingStep = null
  chatState.error = ''
  chatState.pendingDeleteId = null
}

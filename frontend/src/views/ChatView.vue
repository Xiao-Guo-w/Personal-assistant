<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

import MarkdownText from '@/components/MarkdownText.vue'
import ReminderInbox from '@/components/ReminderInbox.vue'
import { chatState, createSession, currentSession, sendMessage } from '@/stores/chat'

const draft = ref('')
const scroller = ref<HTMLElement | null>(null)

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const element = scroller.value
  if (element) element.scrollTop = element.scrollHeight
}

watch(() => [chatState.messages.length, chatState.sending], scrollToBottom)
watch(() => chatState.currentSessionId, scrollToBottom)

async function submit(): Promise<void> {
  const text = draft.value.trim()
  if (!text || chatState.sending) return
  draft.value = ''
  await sendMessage(text)
}

function onKeydown(event: KeyboardEvent): void {
  // 中文输入法选词时的 Enter 不能当成发送
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void submit()
  }
}

async function handleConfirm(approve: boolean): Promise<void> {
  await sendMessage(approve ? '确认' : '取消')
}
</script>

<template>
  <div class="page chat-page">
    <header class="page-head">
      <h1>{{ currentSession?.title || '新对话' }}</h1>
      <span class="muted small">Agent 在执行有风险的操作前会先向你确认</span>
    </header>

    <ReminderInbox />

    <div ref="scroller" class="chat-scroll">
      <div v-if="!chatState.currentSessionId" class="empty-state">
        <p>还没有选中会话。</p>
        <button type="button" class="btn btn-primary" @click="createSession()">🆕 新建会话</button>
      </div>

      <p v-else-if="chatState.loadingMessages" class="muted">正在加载会话…</p>

      <template v-else>
        <div v-if="!chatState.messages.length" class="empty-state">
          <p>开始对话吧，比如：</p>
          <ul class="feature-list">
            <li>「帮我约张三明天下午 3 点开会」</li>
            <li>「提醒我今晚 8 点交周报」</li>
            <li>「以后默认会议 1 小时」</li>
          </ul>
        </div>

        <div
          v-for="(message, index) in chatState.messages"
          :key="index"
          class="msg"
          :class="`msg-${message.role}`"
        >
          <div class="avatar">{{ message.role === 'user' ? '🧑' : '🤖' }}</div>
          <div class="bubble" :class="{ failed: message.failed }">
            <MarkdownText :content="message.content" />
          </div>
        </div>

        <div v-if="chatState.sending" class="msg msg-assistant">
          <div class="avatar">🤖</div>
          <div class="bubble typing"><span /><span /><span /></div>
        </div>
      </template>
    </div>

    <div v-if="chatState.status === 'WAITING_CONFIRMATION'" class="confirm-bar">
      <span>⚠️ Agent 需要你确认这次操作</span>
      <div class="row">
        <button
          type="button"
          class="btn btn-primary btn-sm"
          :disabled="chatState.sending"
          @click="handleConfirm(true)"
        >
          ✅ 确认
        </button>
        <button
          type="button"
          class="btn btn-ghost btn-sm"
          :disabled="chatState.sending"
          @click="handleConfirm(false)"
        >
          ❌ 取消
        </button>
      </div>
    </div>

    <div class="composer">
      <textarea
        v-model="draft"
        class="composer-input"
        rows="1"
        placeholder="输入你的指令…（Enter 发送，Shift + Enter 换行）"
        :disabled="!chatState.currentSessionId"
        @keydown="onKeydown"
      />
      <button
        type="button"
        class="btn btn-primary"
        :disabled="chatState.sending || !draft.trim() || !chatState.currentSessionId"
        @click="submit"
      >
        {{ chatState.sending ? '思考中…' : '发送' }}
      </button>
    </div>
  </div>
</template>

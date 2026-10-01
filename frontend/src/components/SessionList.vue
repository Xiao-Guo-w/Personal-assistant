<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { pushToast } from '@/composables/useToast'
import { chatState, deleteSession, openSession } from '@/stores/chat'

const router = useRouter()
const route = useRoute()
const confirmingId = ref<string | null>(null)

async function select(sessionId: string): Promise<void> {
  confirmingId.value = null
  if (route.name !== 'chat') await router.push({ name: 'chat' })
  await openSession(sessionId)
}

async function confirmDelete(sessionId: string, title: string): Promise<void> {
  const ok = await deleteSession(sessionId)
  confirmingId.value = null
  if (ok) pushToast(`已删除「${title}」`, 'success', '🗑️')
  else pushToast(chatState.error || '删除失败', 'error', '⚠️')
}
</script>

<template>
  <div class="session-list">
    <p v-if="!chatState.sessions.length" class="muted small">还没有会话，点上面的按钮新建一个。</p>

    <div v-for="session in chatState.sessions" :key="session.id" class="session-item-wrap">
      <div
        class="session-item"
        :class="{ active: session.id === chatState.currentSessionId }"
      >
        <button type="button" class="session-title" :title="session.title" @click="select(session.id)">
          {{ session.title || '新会话' }}
        </button>
        <button
          type="button"
          class="icon-btn"
          title="删除此会话"
          @click="confirmingId = confirmingId === session.id ? null : session.id"
        >
          🗑️
        </button>
      </div>

      <div v-if="confirmingId === session.id" class="confirm-box">
        <p class="small">确定删除「{{ session.title || '新会话' }}」？不可撤销。</p>
        <div class="confirm-actions">
          <button
            type="button"
            class="btn btn-danger btn-xs"
            @click="confirmDelete(session.id, session.title)"
          >
            确认删除
          </button>
          <button type="button" class="btn btn-ghost btn-xs" @click="confirmingId = null">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

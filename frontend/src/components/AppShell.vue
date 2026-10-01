<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import ReminderPanel from '@/components/ReminderPanel.vue'
import SessionList from '@/components/SessionList.vue'
import { pushToast } from '@/composables/useToast'
import { authState, displayName, logout, restartOnboarding } from '@/stores/auth'
import { chatState, createSession, loadSessions, openSession, resetChat } from '@/stores/chat'
import { resetReminders, unreadCount } from '@/stores/reminders'

const router = useRouter()
const route = useRoute()

const navItems = [
  { name: 'chat', label: '聊天', icon: '💬' },
  { name: 'memory', label: '长期记忆', icon: '📖' },
  { name: 'settings', label: '集成设置', icon: '⚙️' },
] as const

onMounted(async () => {
  if (!chatState.sessions.length) await loadSessions()
  // 默认打开最近一次会话，省一次点击
  if (!chatState.currentSessionId && chatState.sessions.length) {
    await openSession(chatState.sessions[0].id)
  }
})

async function handleNewSession(): Promise<void> {
  const id = await createSession()
  if (!id) {
    pushToast(chatState.error || '新建会话失败', 'error', '⚠️')
    return
  }
  if (route.name !== 'chat') await router.push({ name: 'chat' })
}

async function handleLogout(): Promise<void> {
  await logout()
  resetChat()
  resetReminders()
  await router.replace({ name: 'login' })
}

async function handleRestartOnboarding(): Promise<void> {
  restartOnboarding()
  await router.push({ name: 'onboarding' })
}
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-icon">🤖</span>
        <span>个人事务助理</span>
      </div>

      <div class="user-card">
        <div class="user-name">👤 {{ displayName }}</div>
        <div class="user-meta">
          @{{ authState.user?.username }} · {{ authState.user?.timezone }}
        </div>
      </div>

      <button type="button" class="btn btn-primary btn-block" @click="handleNewSession">
        🆕 新建会话
      </button>

      <SessionList />

      <hr class="divider" />

      <ReminderPanel />

      <hr class="divider" />

      <nav class="nav">
        <RouterLink
          v-for="item in navItems"
          :key="item.name"
          :to="{ name: item.name }"
          class="nav-item"
          active-class="active"
        >
          <span>{{ item.icon }} {{ item.label }}</span>
          <span v-if="item.name === 'chat' && unreadCount" class="badge">{{ unreadCount }}</span>
        </RouterLink>
      </nav>

      <div class="sidebar-footer">
        <button type="button" class="btn btn-ghost btn-block" @click="handleRestartOnboarding">
          🎬 重新引导
        </button>
        <button type="button" class="btn btn-ghost btn-block" @click="handleLogout">登出</button>
      </div>
    </aside>

    <main class="main">
      <slot />
    </main>
  </div>
</template>

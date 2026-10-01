import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { getToken, onUnauthorized } from '@/api'
import { authState, refreshMe, resetAuth } from '@/stores/auth'
import { resetChat } from '@/stores/chat'
import { resetReminders } from '@/stores/reminders'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, bare: true },
  },
  {
    path: '/onboarding',
    name: 'onboarding',
    component: () => import('@/views/OnboardingView.vue'),
    meta: { bare: true },
  },
  { path: '/', name: 'chat', component: () => import('@/views/ChatView.vue') },
  { path: '/memory', name: 'memory', component: () => import('@/views/MemoryView.vue') },
  { path: '/settings', name: 'settings', component: () => import('@/views/SettingsView.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  if (!getToken()) {
    return to.meta.public ? true : { name: 'login' }
  }

  if (!authState.user) {
    try {
      await refreshMe()
    } catch {
      resetAuth()
      return { name: 'login' }
    }
  }

  // 未完成引导的用户先走引导流程
  if (!authState.user?.onboarded) {
    return to.name === 'onboarding' ? true : { name: 'onboarding' }
  }

  if (to.meta.public || to.name === 'onboarding') return { name: 'chat' }
  return true
})

onUnauthorized(() => {
  resetAuth()
  resetChat()
  resetReminders()
  if (router.currentRoute.value.name !== 'login') {
    void router.replace({ name: 'login' })
  }
})

export default router

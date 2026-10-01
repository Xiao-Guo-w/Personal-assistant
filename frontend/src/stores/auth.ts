import { computed, reactive } from 'vue'

import { authApi, errorMessage, getToken, setToken } from '@/api'
import type { User } from '@/api/types'

interface AuthState {
  token: string
  user: User | null
  busy: boolean
  error: string
}

export const authState = reactive<AuthState>({
  token: getToken(),
  user: null,
  busy: false,
  error: '',
})

export const isAuthenticated = computed(() => Boolean(authState.token && authState.user))
export const displayName = computed(
  () => authState.user?.display_name || authState.user?.username || '访客',
)

function applyAuth(token: string, user: User): void {
  setToken(token)
  authState.token = token
  authState.user = user
  authState.error = ''
}

export async function login(username: string, password: string): Promise<boolean> {
  authState.busy = true
  authState.error = ''
  try {
    const data = await authApi.login(username.trim(), password)
    applyAuth(data.token, data.user)
    return true
  } catch (error) {
    authState.error = errorMessage(error)
    return false
  } finally {
    authState.busy = false
  }
}

export async function register(payload: {
  username: string
  password: string
  display_name: string
  timezone: string
}): Promise<boolean> {
  authState.busy = true
  authState.error = ''
  try {
    const data = await authApi.register({
      ...payload,
      username: payload.username.trim(),
      display_name: payload.display_name.trim(),
    })
    applyAuth(data.token, data.user)
    return true
  } catch (error) {
    authState.error = errorMessage(error)
    return false
  } finally {
    authState.busy = false
  }
}

export async function refreshMe(): Promise<User> {
  const user = await authApi.me()
  authState.user = user
  return user
}

export async function updateProfile(payload: {
  display_name?: string
  timezone?: string
}): Promise<void> {
  authState.user = await authApi.updateProfile(payload)
}

export async function completeOnboarding(payload: {
  timezone?: string
  default_meeting_duration?: number
}): Promise<void> {
  await authApi.completeOnboarding(payload)
  if (authState.user) authState.user = { ...authState.user, onboarded: true }
}

/** 本地把用户标记为「未引导」，用于侧边栏的「重新引导」。 */
export function restartOnboarding(): void {
  if (authState.user) authState.user = { ...authState.user, onboarded: false }
}

export function resetAuth(): void {
  setToken(null)
  authState.token = ''
  authState.user = null
  authState.error = ''
}

export async function logout(): Promise<void> {
  try {
    await authApi.logout()
  } catch {
    /* 后端不可用时也要让本地登出成功 */
  }
  resetAuth()
}

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { authState, login, register } from '@/stores/auth'

const router = useRouter()
const tab = ref<'login' | 'register'>('login')

const form = reactive({
  username: '',
  password: '',
  display_name: '',
  timezone: 'Asia/Shanghai',
})

async function afterLogin(): Promise<void> {
  await router.replace({ name: authState.user?.onboarded ? 'chat' : 'onboarding' })
}

async function submitLogin(): Promise<void> {
  if (!form.username.trim() || !form.password) {
    authState.error = '请填写用户名和密码'
    return
  }
  if (await login(form.username, form.password)) await afterLogin()
}

async function submitRegister(): Promise<void> {
  if (form.username.trim().length < 3) {
    authState.error = '用户名至少 3 个字符'
    return
  }
  if (form.password.length < 6) {
    authState.error = '密码至少 6 位'
    return
  }
  if (
    await register({
      username: form.username,
      password: form.password,
      display_name: form.display_name,
      timezone: form.timezone || 'Asia/Shanghai',
    })
  ) {
    await afterLogin()
  }
}

function switchTab(next: 'login' | 'register'): void {
  tab.value = next
  authState.error = ''
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <header class="auth-header">
        <div class="auth-logo">🤖</div>
        <h1>个人事务助理</h1>
        <p class="muted">日程、邮件、Notion、提醒，一句话交给 Agent。</p>
      </header>

      <div class="tabs">
        <button
          type="button"
          class="tab"
          :class="{ active: tab === 'login' }"
          @click="switchTab('login')"
        >
          登录
        </button>
        <button
          type="button"
          class="tab"
          :class="{ active: tab === 'register' }"
          @click="switchTab('register')"
        >
          注册
        </button>
      </div>

      <form v-if="tab === 'login'" class="stack" @submit.prevent="submitLogin">
        <label class="field">
          <span class="label">用户名</span>
          <input v-model="form.username" class="input" autocomplete="username" placeholder="your_name" />
        </label>

        <label class="field">
          <span class="label">密码</span>
          <input
            v-model="form.password"
            class="input"
            type="password"
            autocomplete="current-password"
            placeholder="••••••"
          />
        </label>

        <p v-if="authState.error" class="alert alert-error compact">{{ authState.error }}</p>

        <button type="submit" class="btn btn-primary btn-block" :disabled="authState.busy">
          {{ authState.busy ? '登录中…' : '登录' }}
        </button>
      </form>

      <form v-else class="stack" @submit.prevent="submitRegister">
        <label class="field">
          <span class="label">用户名</span>
          <input v-model="form.username" class="input" autocomplete="username" placeholder="至少 3 个字符" />
        </label>

        <label class="field">
          <span class="label">密码</span>
          <input
            v-model="form.password"
            class="input"
            type="password"
            autocomplete="new-password"
            placeholder="至少 6 位"
          />
        </label>

        <label class="field">
          <span class="label">显示名（可选）</span>
          <input v-model="form.display_name" class="input" placeholder="张三" />
        </label>

        <label class="field">
          <span class="label">时区</span>
          <input v-model="form.timezone" class="input" placeholder="Asia/Shanghai" />
        </label>

        <p v-if="authState.error" class="alert alert-error compact">{{ authState.error }}</p>

        <button type="submit" class="btn btn-primary btn-block" :disabled="authState.busy">
          {{ authState.busy ? '注册中…' : '注册并开始' }}
        </button>
      </form>
    </div>
  </div>
</template>

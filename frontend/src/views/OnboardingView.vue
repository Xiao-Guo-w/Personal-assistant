<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { errorMessage, integrationApi } from '@/api'
import type { IntegrationStatus } from '@/api/types'
import { authState, completeOnboarding, displayName, refreshMe } from '@/stores/auth'
import { createSession } from '@/stores/chat'

const router = useRouter()

const TIMEZONES = [
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Asia/Singapore',
  'America/New_York',
  'America/Los_Angeles',
  'Europe/London',
  'Europe/Berlin',
  'UTC',
]

const DURATIONS = [30, 60, 90, 120]

const total = 4
const step = ref(0)
const timezone = ref(authState.user?.timezone || 'Asia/Shanghai')
const duration = ref(60)
const status = ref<IntegrationStatus | null>(null)
const busy = ref(false)
const error = ref('')

const progress = computed(() => `${(step.value / (total - 1)) * 100}%`)

onMounted(() => {
  void loadStatus()
})

async function loadStatus(): Promise<void> {
  try {
    status.value = await integrationApi.status()
  } catch {
    status.value = null
  }
}

function goStep(next: number): void {
  step.value = next
  error.value = ''
}

async function finish(skipPreferences = false): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    await completeOnboarding(
      skipPreferences
        ? {}
        : { timezone: timezone.value, default_meeting_duration: duration.value },
    )
    await refreshMe().catch(() => undefined)
    await createSession()
    await router.replace({ name: 'chat' })
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="onboarding-page">
    <div class="onboarding-card">
      <div class="progress">
        <div class="progress-bar" :style="{ width: progress }" />
      </div>
      <p class="muted small">第 {{ step + 1 }} / {{ total }} 步</p>

      <!-- Step 0：欢迎 -->
      <section v-if="step === 0" class="stack">
        <h1>👋 欢迎使用个人事务助理</h1>
        <p>你好，<strong>{{ displayName }}</strong>！我是你的事务助理，可以帮你：</p>
        <ul class="feature-list">
          <li>📅 <strong>管理日程</strong>：「帮我约张三明天下午 3 点开会」</li>
          <li>📧 <strong>处理邮件</strong>：「查一下我最近关于方案的邮件」</li>
          <li>⏰ <strong>设置提醒</strong>：「提醒我今晚 8 点交周报」</li>
          <li>📚 <strong>问自己的资料</strong>：「在知识库里查一下合同里的付款条款」</li>
        </ul>
        <p class="muted">
          执行有风险的操作前（如发邮件、创建日程）我会先请你确认，也会记住你告诉我的长期偏好。
        </p>
        <div class="row">
          <button type="button" class="btn btn-primary" @click="goStep(1)">开始设置 →</button>
          <button type="button" class="btn btn-ghost" :disabled="busy" @click="finish(true)">
            跳过全部
          </button>
        </div>
      </section>

      <!-- Step 1：基础偏好 -->
      <section v-else-if="step === 1" class="stack">
        <h1>🌏 基础偏好</h1>
        <p class="muted">这些偏好会跨会话生效，随时可以在「长期记忆」里修改。</p>

        <label class="field">
          <span class="label">你的时区</span>
          <select v-model="timezone" class="input">
            <option v-for="zone in TIMEZONES" :key="zone" :value="zone">{{ zone }}</option>
          </select>
        </label>

        <div class="field">
          <span class="label">默认会议时长</span>
          <div class="chip-row">
            <button
              v-for="item in DURATIONS"
              :key="item"
              type="button"
              class="chip"
              :class="{ active: duration === item }"
              @click="duration = item"
            >
              {{ item }} 分钟
            </button>
          </div>
        </div>
        <p class="muted small">你说「约个会」但没提时长时，Agent 会用这个默认值。</p>

        <div class="row">
          <button type="button" class="btn btn-ghost" @click="goStep(0)">← 上一步</button>
          <button type="button" class="btn btn-ghost" @click="goStep(2)">跳过</button>
          <button type="button" class="btn btn-primary" @click="goStep(2)">下一步 →</button>
        </div>
      </section>

      <!-- Step 2：收信邮箱 -->
      <section v-else-if="step === 2" class="stack">
        <h1>📧 绑定收信邮箱（可选）</h1>
        <p class="muted">
          日程和提醒都建在系统内部，到点通过你的 QQ 邮箱通知。现在跳过也行，
          之后可以在「⚙️ 设置 → QQ 邮箱」里随时绑定。
        </p>

        <div class="integration-grid">
          <div class="card">
            <h3>QQ 邮箱</h3>
            <p v-if="status?.email.connected" class="alert alert-success compact">
              ✅ 已绑定：{{ status.email.address }}
            </p>
            <template v-else>
              <p class="muted small">
                尚未绑定。绑定方式：登录 mail.qq.com →「设置」→「账户」→ 开启
                IMAP/SMTP 服务 → 生成 16 位授权码，填到「设置 → QQ 邮箱」里。
              </p>
            </template>
          </div>

          <div class="card">
            <h3>不绑定会怎样？</h3>
            <p class="muted small">
              提醒仍然会触发，但只能在应用内的提醒收件箱里看到，不会收到邮件；
              发送邮件、定时发邮件这类功能也无法使用。
            </p>
          </div>
        </div>

        <div class="row">
          <button type="button" class="btn btn-ghost" @click="loadStatus">🔄 刷新状态</button>
          <span class="muted small">💡 绑定入口在「⚙️ 设置 → QQ 邮箱」。</span>
        </div>

        <p v-if="error" class="alert alert-error compact">{{ error }}</p>

        <div class="row">
          <button type="button" class="btn btn-ghost" @click="goStep(1)">← 上一步</button>
          <button type="button" class="btn btn-ghost" @click="goStep(3)">跳过</button>
          <button type="button" class="btn btn-primary" @click="goStep(3)">下一步 →</button>
        </div>
      </section>

      <!-- Step 3：完成 -->
      <section v-else class="stack">
        <h1>🎉 设置完成</h1>
        <p>一切就绪，现在你可以：</p>
        <ul class="feature-list">
          <li>直接对 Agent 说「帮我约张三明天下午 3 点开会」</li>
          <li>在左侧「⚙️ 集成设置」里继续配置其他集成</li>
          <li>在「📖 长期记忆」里查看 Agent 记住的偏好</li>
        </ul>
        <p v-if="error" class="alert alert-error compact">{{ error }}</p>
        <button type="button" class="btn btn-primary btn-block" :disabled="busy" @click="finish(false)">
          {{ busy ? '正在进入…' : '进入聊天 →' }}
        </button>
      </section>
    </div>
  </div>
</template>

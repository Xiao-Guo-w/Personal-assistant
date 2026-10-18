<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { errorMessage, integrationApi } from '@/api'
import type { EmailConfig, IntegrationStatus } from '@/api/types'
import { pushToast } from '@/composables/useToast'
import { authState, updateProfile } from '@/stores/auth'
import { refreshReminders } from '@/stores/reminders'
import { refreshSchedules } from '@/stores/schedules'
import { timezoneGroups } from '@/utils/timezone'

type Tab = 'email' | 'account'

const tab = ref<Tab>('email')

const status = ref<IntegrationStatus | null>(null)
const loading = ref(true)
const error = ref('')

const emailConfig = ref<EmailConfig | null>(null)
const emailAddress = ref('')
const emailAuthCode = ref('')
const savingEmail = ref(false)

const profileName = ref('')
const profileTimezone = ref('')
const savingProfile = ref(false)

const email = computed(() => status.value?.email)
const profileTimezoneGroups = computed(() => timezoneGroups(profileTimezone.value))

onMounted(async () => {
  await loadStatus()
  await loadEmail()
  profileName.value = authState.user?.display_name ?? ''
  profileTimezone.value = authState.user?.timezone ?? 'Asia/Shanghai'
})

async function loadStatus(): Promise<void> {
  loading.value = true
  try {
    status.value = await integrationApi.status()
    error.value = ''
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

async function loadEmail(): Promise<void> {
  try {
    emailConfig.value = await integrationApi.emailConfig()
    emailAddress.value = emailConfig.value?.address ?? ''
  } catch (err) {
    error.value = errorMessage(err)
  }
}

async function saveEmail(): Promise<void> {
  if (!emailAddress.value.trim() || !emailAuthCode.value.trim()) {
    pushToast('邮箱地址和授权码都不能为空', 'error', '⚠️')
    return
  }
  savingEmail.value = true
  try {
    await integrationApi.saveEmail(emailAddress.value.trim(), emailAuthCode.value.trim())
    emailAuthCode.value = ''
    pushToast('邮箱配置已保存', 'success', '💾')
    await Promise.all([loadEmail(), loadStatus(), refreshReminders(), refreshSchedules()])
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  } finally {
    savingEmail.value = false
  }
}

async function deleteEmail(): Promise<void> {
  try {
    await integrationApi.deleteEmail()
    emailAuthCode.value = ''
    pushToast('已删除邮箱配置', 'success', '🗑️')
    await Promise.all([loadEmail(), loadStatus()])
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  }
}

async function saveProfile(): Promise<void> {
  savingProfile.value = true
  try {
    await updateProfile({
      display_name: profileName.value.trim(),
      timezone: profileTimezone.value.trim() || undefined,
    })
    pushToast('资料已更新', 'success', '💾')
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  } finally {
    savingProfile.value = false
  }
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>⚙️ 设置</h1>
        <p class="muted small">日程与提醒都存在系统内部，只需要绑定一个收信邮箱。</p>
      </div>
      <button type="button" class="btn btn-ghost btn-sm" @click="loadStatus">🔄 刷新状态</button>
    </header>

    <p v-if="error" class="alert alert-error">{{ error }}</p>

    <div class="tabs">
      <button type="button" class="tab" :class="{ active: tab === 'email' }" @click="tab = 'email'">
        QQ 邮箱
      </button>
      <button type="button" class="tab" :class="{ active: tab === 'account' }" @click="tab = 'account'">
        账户
      </button>
    </div>

    <p v-if="loading" class="muted">加载中…</p>

    <!-- ============ QQ 邮箱 ============ -->
    <section v-else-if="tab === 'email'" class="stack">
      <div class="card">
        <h3>QQ 邮箱</h3>
        <p class="muted small">
          日程提醒、定时提醒和邮件发送都走这个邮箱。QQ 邮箱官方未提供第三方授权接口，
          需要手动填写授权码。
        </p>

        <details class="details">
          <summary>📖 如何获取 QQ 邮箱授权码？</summary>
          <ol class="ordered">
            <li>登录 <a href="https://mail.qq.com" target="_blank" rel="noreferrer">mail.qq.com</a></li>
            <li>点击「设置」→「账户」</li>
            <li>找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」</li>
            <li>开启「IMAP/SMTP 服务」（需短信验证）</li>
            <li>点击「生成授权码」，获得 16 位授权码</li>
          </ol>
        </details>

        <p v-if="email?.connected" class="alert alert-success compact">
          ✅ 已绑定：{{ email.address }}，提醒会发到这里
        </p>
        <p v-else class="alert alert-warn compact">
          ⚠️ 还没绑定邮箱：到点的提醒只能在应用内看到，收不到邮件
        </p>

        <label class="field">
          <span class="label">QQ 邮箱地址</span>
          <input v-model="emailAddress" class="input" placeholder="your_qq@qq.com" />
        </label>

        <label class="field">
          <span class="label">授权码</span>
          <input
            v-model="emailAuthCode"
            class="input"
            type="password"
            placeholder="16 位授权码"
            autocomplete="new-password"
          />
        </label>

        <div class="row">
          <button type="button" class="btn btn-primary" :disabled="savingEmail" @click="saveEmail">
            💾 {{ savingEmail ? '保存中…' : '保存邮箱配置' }}
          </button>
          <button v-if="emailConfig?.configured" type="button" class="btn btn-ghost" @click="deleteEmail">
            🗑️ 删除邮箱配置
          </button>
        </div>
      </div>
    </section>

    <!-- ============ 账户 ============ -->
    <section v-else class="stack">
      <div class="card">
        <h3>账户资料</h3>
        <p class="muted small">
          用户名 <strong>@{{ authState.user?.username }}</strong> 不可修改；显示名与时区随时可改。
        </p>

        <label class="field">
          <span class="label">显示名</span>
          <input v-model="profileName" class="input" placeholder="张三" />
        </label>

        <label class="field">
          <span class="label">时区</span>
          <select v-model="profileTimezone" class="input">
            <optgroup
              v-for="group in profileTimezoneGroups"
              :key="group.label"
              :label="group.label"
            >
              <option v-for="zone in group.options" :key="zone.value" :value="zone.value">
                {{ zone.label }}
              </option>
            </optgroup>
          </select>
          <span class="muted small">日程和提醒的时间会按这个时区显示与换算。</span>
        </label>

        <button type="button" class="btn btn-primary" :disabled="savingProfile" @click="saveProfile">
          💾 {{ savingProfile ? '保存中…' : '保存资料' }}
        </button>
      </div>
    </section>
  </div>
</template>

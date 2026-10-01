<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { errorMessage, integrationApi } from '@/api'
import type { EmailConfig, IntegrationStatus, NotionPage } from '@/api/types'
import { pushToast } from '@/composables/useToast'
import { authState, updateProfile } from '@/stores/auth'
import { refreshReminders } from '@/stores/reminders'

type Tab = 'notion' | 'feishu' | 'email' | 'account'

const tab = ref<Tab>('notion')

const status = ref<IntegrationStatus | null>(null)
const loading = ref(true)
const error = ref('')

const notionPages = ref<NotionPage[]>([])
const selectedPageId = ref('')
const pagesLoaded = ref(false)
const loadingPages = ref(false)

const emailConfig = ref<EmailConfig | null>(null)
const emailAddress = ref('')
const emailAuthCode = ref('')
const savingEmail = ref(false)

const profileName = ref('')
const profileTimezone = ref('')
const savingProfile = ref(false)

const notion = computed(() => status.value?.notion)
const feishu = computed(() => status.value?.feishu_calendar)
const email = computed(() => status.value?.email)

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

async function connect(provider: 'feishu' | 'notion'): Promise<void> {
  try {
    const { authorize_url: authorizeUrl } = await integrationApi.authorizeUrl(provider)
    if (!authorizeUrl) {
      pushToast('授权链接为空，请检查后端 OAuth 配置', 'error', '⚠️')
      return
    }
    window.open(authorizeUrl, '_blank', 'noopener,noreferrer')
    pushToast('已打开授权页面，完成后点「刷新状态」', 'info', '🔗')
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  }
}

async function disconnect(provider: string, label: string): Promise<void> {
  try {
    await integrationApi.disconnect(provider)
    pushToast(`已断开 ${label}`, 'success', '🔌')
    await loadStatus()
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  }
}

async function loadNotionPages(): Promise<void> {
  loadingPages.value = true
  try {
    const data = await integrationApi.notionPages()
    notionPages.value = data.pages
    pagesLoaded.value = true
    if (!data.pages.length) {
      pushToast('未找到页面，请在 Notion 授权时勾选要使用的页面', 'error', '⚠️')
    }
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  } finally {
    loadingPages.value = false
  }
}

async function setDefaultPage(): Promise<void> {
  if (!selectedPageId.value) return
  try {
    const result = await integrationApi.setNotionParentPage(selectedPageId.value)
    pushToast(`默认父页面已设为「${result.title || result.page_id}」`, 'success', '💾')
    await loadStatus()
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
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
    await Promise.all([loadEmail(), loadStatus(), refreshReminders()])
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
        <h1>⚙️ 集成设置</h1>
        <p class="muted small">点击「连接」完成授权，无需手动查找凭证。</p>
      </div>
      <button type="button" class="btn btn-ghost btn-sm" @click="loadStatus">🔄 刷新状态</button>
    </header>

    <p v-if="error" class="alert alert-error">{{ error }}</p>

    <div class="tabs">
      <button type="button" class="tab" :class="{ active: tab === 'notion' }" @click="tab = 'notion'">
        Notion
      </button>
      <button type="button" class="tab" :class="{ active: tab === 'feishu' }" @click="tab = 'feishu'">
        飞书日历
      </button>
      <button type="button" class="tab" :class="{ active: tab === 'email' }" @click="tab = 'email'">
        QQ 邮箱
      </button>
      <button type="button" class="tab" :class="{ active: tab === 'account' }" @click="tab = 'account'">
        账户
      </button>
    </div>

    <p v-if="loading" class="muted">加载中…</p>

    <!-- ============ Notion ============ -->
    <section v-else-if="tab === 'notion'" class="stack">
      <div class="card">
        <h3>Notion</h3>
        <p class="muted small">连接后 Agent 可以读写你的 Notion 页面。</p>

        <p v-if="notion?.connected" class="alert alert-success compact">
          ✅ 已连接{{ notion.workspace_name ? `：${notion.workspace_name}` : '' }}
        </p>
        <template v-else>
          <p class="alert alert-info compact">尚未连接 Notion</p>
          <button type="button" class="btn btn-primary" @click="connect('notion')">🔗 连接 Notion</button>
        </template>

        <button
          v-if="notion?.connected"
          type="button"
          class="btn btn-ghost"
          @click="disconnect('notion', 'Notion')"
        >
          🔌 断开 Notion 连接
        </button>
      </div>

      <div v-if="notion?.connected" class="card">
        <h3>默认父页面</h3>
        <p class="muted small">Agent 创建的 Notion 页面都会作为这个页面的子页面写入。</p>

        <div class="row">
          <button type="button" class="btn" :disabled="loadingPages" @click="loadNotionPages">
            🔍 {{ loadingPages ? '查询中…' : '列出可写入的页面' }}
          </button>
        </div>

        <template v-if="notionPages.length">
          <label class="field">
            <span class="label">选择默认父页面</span>
            <select v-model="selectedPageId" class="input">
              <option value="" disabled>请选择…</option>
              <option v-for="page in notionPages" :key="page.id" :value="page.id">
                {{ page.is_top_level ? '🏠 ' : '' }}{{ page.title || '(无标题)' }} · {{ page.id }}
              </option>
            </select>
          </label>
          <button type="button" class="btn btn-primary" :disabled="!selectedPageId" @click="setDefaultPage">
            💾 设为默认
          </button>
        </template>

        <p v-else-if="pagesLoaded" class="muted small">没有可选页面。</p>

        <p v-if="notion.default_parent_page_id" class="muted small">
          当前默认父页面：<code>{{ notion.default_parent_page_id }}</code>
        </p>
        <p v-else class="alert alert-warn compact">
          还没有设置默认父页面，Agent 目前无法创建 Notion 页面。
        </p>
      </div>
    </section>

    <!-- ============ 飞书日历 ============ -->
    <section v-else-if="tab === 'feishu'" class="stack">
      <div class="card">
        <h3>飞书日历</h3>
        <p class="muted small">连接后 Agent 可以帮你查询和创建日程。</p>

        <p v-if="feishu?.connected" class="alert alert-success compact">
          ✅ 已连接{{ feishu.name ? `：${feishu.name}` : '' }}
        </p>
        <template v-else>
          <p class="alert alert-info compact">尚未连接飞书日历</p>
          <button type="button" class="btn btn-primary" @click="connect('feishu')">🔗 连接飞书日历</button>
        </template>

        <button
          v-if="feishu?.connected"
          type="button"
          class="btn btn-ghost"
          @click="disconnect('feishu_calendar', '飞书日历')"
        >
          🔌 断开飞书日历连接
        </button>

        <p class="muted small">💡 飞书授权页支持扫码登录，可以用飞书 App 扫码完成授权。</p>
      </div>
    </section>

    <!-- ============ QQ 邮箱 ============ -->
    <section v-else-if="tab === 'email'" class="stack">
      <div class="card">
        <h3>QQ 邮箱</h3>
        <p class="muted small">QQ 邮箱官方未提供第三方 OAuth 接口，需要手动获取授权码。</p>

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
          ✅ 已配置：{{ email.address }}
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
          <input v-model="profileTimezone" class="input" placeholder="Asia/Shanghai" />
        </label>

        <button type="button" class="btn btn-primary" :disabled="savingProfile" @click="saveProfile">
          💾 {{ savingProfile ? '保存中…' : '保存资料' }}
        </button>
      </div>
    </section>
  </div>
</template>

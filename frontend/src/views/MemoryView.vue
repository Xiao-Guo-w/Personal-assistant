<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { errorMessage, memoryApi } from '@/api'
import type { MemoryItem } from '@/api/types'
import { pushToast } from '@/composables/useToast'

const CATEGORY_ORDER = ['时间与日程', '常用联系人', '沟通与邮件', '工具与集成', '其他']

const memories = ref<MemoryItem[]>([])
const loading = ref(true)
const error = ref('')
const detailKey = ref<string | null>(null)
const confirmKey = ref<string | null>(null)

const groups = computed(() => {
  const map = new Map<string, MemoryItem[]>()
  for (const item of memories.value) {
    const bucket = map.get(item.category) ?? []
    bucket.push(item)
    map.set(item.category, bucket)
  }

  const known = CATEGORY_ORDER.filter((category) => map.has(category))
  const extra = [...map.keys()].filter((category) => !CATEGORY_ORDER.includes(category))
  return [...known, ...extra].map((category) => ({ category, items: map.get(category) as MemoryItem[] }))
})

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    memories.value = await memoryApi.list()
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

async function remove(item: MemoryItem): Promise<void> {
  confirmKey.value = null
  try {
    await memoryApi.remove(item.key)
    memories.value = memories.value.filter((entry) => entry.key !== item.key)
    pushToast(`已删除：${item.label}`, 'success', '🗑️')
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  }
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>📖 长期记忆</h1>
        <p class="muted small">Agent 记住的关于你的偏好，会跨会话生效。</p>
      </div>
      <button type="button" class="btn btn-ghost btn-sm" @click="load">🔄 刷新</button>
    </header>

    <p v-if="loading" class="muted">加载中…</p>
    <p v-else-if="error" class="alert alert-error">{{ error }}</p>

    <div v-else-if="!memories.length" class="empty-state">
      <p>还没有任何长期记忆。</p>
      <p class="muted small">试试对 Agent 说：</p>
      <ul class="feature-list">
        <li>「以后默认会议 1 小时」</li>
        <li>「我的时区是 Asia/Shanghai」</li>
        <li>「张三的邮箱是 zhangsan@example.com」</li>
      </ul>
    </div>

    <template v-else>
      <p class="muted small">共 {{ memories.length }} 条记忆</p>

      <section v-for="group in groups" :key="group.category" class="memory-group">
        <h2 class="group-title">{{ group.category }}（{{ group.items.length }}）</h2>

        <article v-for="item in group.items" :key="item.key" class="card memory-card">
          <div class="memory-main">
            <h3>{{ item.icon }} {{ item.label }}</h3>
            <p class="memory-value">{{ item.display_value }}</p>
          </div>

          <div class="memory-actions">
            <button
              type="button"
              class="btn btn-ghost btn-xs"
              @click="detailKey = detailKey === item.key ? null : item.key"
            >
              详情
            </button>
            <button type="button" class="btn btn-ghost btn-xs" @click="confirmKey = item.key">
              🗑️ 删除
            </button>
          </div>

          <dl v-if="detailKey === item.key" class="memory-detail">
            <dt>内部键名</dt>
            <dd>{{ item.key }}</dd>
            <dt>原始值</dt>
            <dd>{{ item.raw_value }}</dd>
            <dt>更新时间</dt>
            <dd>{{ item.updated_at || '未知' }}</dd>
          </dl>

          <div v-if="confirmKey === item.key" class="confirm-box">
            <p class="small">确定删除这条记忆？Agent 之后就不会再记住它了。</p>
            <div class="confirm-actions">
              <button type="button" class="btn btn-danger btn-xs" @click="remove(item)">确认删除</button>
              <button type="button" class="btn btn-ghost btn-xs" @click="confirmKey = null">取消</button>
            </div>
          </div>
        </article>
      </section>
    </template>
  </div>
</template>

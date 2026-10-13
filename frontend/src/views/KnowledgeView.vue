<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { errorMessage, kbApi } from '@/api'
import type { KbChunk, KbDocument, KbHit } from '@/api/types'
import { pushToast } from '@/composables/useToast'
import {
  activeDocuments,
  documentGroups,
  kbState,
  loadStats,
  refresh,
  removeDocument,
  retryDocument,
  stopKbPolling,
  uploadFile,
} from '@/stores/kb'

const ACCEPT = '.pdf,.docx,.md,.markdown,.txt'
const MAX_MB = 20

const tab = ref<'upload' | 'library'>('upload')
const dragging = ref(false)
const busy = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const confirmId = ref<number | null>(null)

const chunkDoc = ref<KbDocument | null>(null)
const chunks = ref<KbChunk[]>([])
const chunksLoading = ref(false)

const searchQuery = ref('')
const searching = ref(false)
const searched = ref(false)
const hits = ref<KbHit[]>([])

const totalCount = computed(() => kbState.documents.length)

onMounted(async () => {
  await refresh()
  void loadStats()
})

onUnmounted(() => stopKbPolling())

function statusText(doc: KbDocument): string {
  if (doc.status === 'pending' || doc.status === 'indexing') return '正在加入知识库中…'
  if (doc.status === 'ready') return '已入库'
  return '入库失败'
}

function statusClass(doc: KbDocument): string {
  if (doc.status === 'ready') return 'ready'
  if (doc.status === 'failed') return 'failed'
  return 'active'
}

/** 入库中的文件不给删：索引 worker 正在写数据，此时删除会撞在它身上。 */
function canDelete(doc: KbDocument): boolean {
  return doc.status === 'ready' || doc.status === 'failed'
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatTime(value: string | null): string {
  if (!value) return ''
  return value.replace('T', ' ').slice(0, 16)
}

async function handleFiles(files: FileList | null): Promise<void> {
  if (!files || !files.length) return
  busy.value = true

  let accepted = 0
  for (const file of Array.from(files)) {
    const message = await uploadFile(file)
    if (message) {
      pushToast(`${file.name}：${message}`, 'error', '⚠️')
    } else {
      accepted += 1
      pushToast(`已加入知识库：${file.name}`, 'success', '📥')
    }
  }

  busy.value = false
  // 清空 input，否则同一个文件出错后无法再次选中重试
  if (fileInput.value) fileInput.value.value = ''
  if (accepted) {
    tab.value = 'upload'
    void loadStats()
  }
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  void handleFiles(event.dataTransfer?.files ?? null)
}

async function handleDelete(doc: KbDocument): Promise<void> {
  confirmId.value = null
  const message = await removeDocument(doc)
  if (message) pushToast(message, 'error', '⚠️')
  else pushToast(`已删除：${doc.title}`, 'success', '🗑️')
}

async function handleRetry(doc: KbDocument): Promise<void> {
  const message = await retryDocument(doc)
  if (message) pushToast(message, 'error', '⚠️')
  else pushToast(`已重新排队：${doc.title}`, 'success', '🔄')
}

async function openChunks(doc: KbDocument): Promise<void> {
  chunkDoc.value = doc
  chunks.value = []
  chunksLoading.value = true
  try {
    chunks.value = await kbApi.chunks(doc.id, 100)
  } catch (error) {
    pushToast(errorMessage(error), 'error', '⚠️')
    chunkDoc.value = null
  } finally {
    chunksLoading.value = false
  }
}

async function runSearch(): Promise<void> {
  const query = searchQuery.value.trim()
  if (!query) return
  searching.value = true
  try {
    const result = await kbApi.search(query)
    hits.value = result.hits
    searched.value = true
  } catch (error) {
    pushToast(errorMessage(error), 'error', '⚠️')
  } finally {
    searching.value = false
  }
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1>📚 知识库</h1>
        <p class="muted small">
          上传的文件会解析、分块后建立索引，之后可以直接向助手提问。
        </p>
      </div>
      <button type="button" class="btn btn-ghost btn-sm" @click="refresh()">🔄 刷新</button>
    </header>

    <div class="tabs">
      <button type="button" class="tab" :class="{ active: tab === 'upload' }" @click="tab = 'upload'">
        上传
        <span v-if="activeDocuments.length">（{{ activeDocuments.length }}）</span>
      </button>
      <button type="button" class="tab" :class="{ active: tab === 'library' }" @click="tab = 'library'">
        我的知识库（{{ totalCount }}）
      </button>
    </div>

    <p v-if="kbState.error" class="alert alert-error">{{ kbState.error }}</p>

    <!-- ================= 上传 ================= -->
    <div v-if="tab === 'upload'" class="stack">
      <div
        class="kb-drop"
        :class="{ dragging }"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
        @click="fileInput?.click()"
      >
        <span class="kb-drop-icon">📥</span>
        <div>把文件拖到这里，或点击选择</div>
        <div class="muted small kb-drop-hint">
          支持 PDF、Word(.docx)、Markdown、纯文本，单个文件不超过 {{ MAX_MB }}MB
        </div>
      </div>

      <input
        ref="fileInput"
        class="sr-only-input"
        type="file"
        multiple
        :accept="ACCEPT"
        @change="handleFiles(($event.target as HTMLInputElement).files)"
      />

      <p v-if="busy" class="muted small">正在上传解析…</p>

      <section v-if="activeDocuments.length" class="card">
        <h3>正在入库</h3>
        <p class="muted small">入库完成后才能在「我的知识库」里删除。</p>
        <div v-for="doc in activeDocuments" :key="doc.id" class="kb-queue-item">
          <div class="kb-row-main">
            <div class="kb-row-title">{{ doc.title }}</div>
            <div class="kb-row-meta">{{ formatSize(doc.size_bytes) }}</div>
          </div>
          <span class="kb-status active">正在加入知识库中…</span>
        </div>
      </section>

      <section class="card">
        <h3>使用提示</h3>
        <ul class="feature-list">
          <li>同一份内容只需要上传一次，重复上传会被拦下来并提示。</li>
          <li>入库过程中请勿删除文件，完成后「我的知识库」里会出现删除按钮。</li>
          <li>上传完可以到「我的知识库」用搜索测试确认内容已经能被检索到。</li>
        </ul>
      </section>
    </div>

    <!-- ================= 展示 ================= -->
    <div v-else class="stack">
      <p v-if="kbState.loading && !totalCount" class="muted">加载中…</p>

      <div v-else-if="!totalCount" class="empty-state">
        <p>还没有任何文件。</p>
        <p class="muted small">切到「上传」标签页，把资料丢进来就能开始提问。</p>
      </div>

      <template v-else>
        <div class="kb-stats">
          <span>文件 {{ totalCount }} 个</span>
          <span>分块 {{ kbState.stats?.chunks ?? 0 }} 个</span>
          <span>
            {{ kbState.stats?.vector_configured ? '语义检索：已启用' : '语义检索：未配置（当前仅关键词匹配）' }}
          </span>
        </div>

        <section v-for="group in documentGroups" :key="group.ext" class="stack">
          <h2 class="group-title">{{ group.icon }} {{ group.label }}（{{ group.items.length }}）</h2>

          <article v-for="doc in group.items" :key="doc.id" class="card">
            <div class="kb-row">
              <div class="kb-row-main">
                <div class="kb-row-title">{{ doc.title }}</div>
                <div class="kb-row-meta">
                  {{ formatSize(doc.size_bytes) }}
                  <template v-if="doc.status === 'ready'">
                    · {{ doc.chunk_count }} 个片段 · {{ formatTime(doc.created_at) }}
                  </template>
                </div>
                <div v-if="doc.status === 'failed' && doc.error" class="kb-row-error">
                  {{ doc.error }}
                </div>
              </div>

              <span class="kb-status" :class="statusClass(doc)">{{ statusText(doc) }}</span>

              <div class="kb-actions">
                <button
                  v-if="doc.status === 'ready'"
                  type="button"
                  class="btn btn-ghost btn-xs"
                  @click="openChunks(doc)"
                >
                  查看片段
                </button>
                <button
                  v-if="doc.status === 'failed'"
                  type="button"
                  class="btn btn-ghost btn-xs"
                  @click="handleRetry(doc)"
                >
                  🔄 重试
                </button>
                <button
                  v-if="canDelete(doc)"
                  type="button"
                  class="btn btn-ghost btn-xs"
                  @click="confirmId = confirmId === doc.id ? null : doc.id"
                >
                  🗑️ 删除
                </button>
              </div>
            </div>

            <div v-if="confirmId === doc.id" class="confirm-box">
              <p class="small">
                确定删除「{{ doc.title }}」？已建立的检索索引会一并清除，
                重新入库需要再次解析与向量化。
              </p>
              <div class="confirm-actions">
                <button type="button" class="btn btn-danger btn-xs" @click="handleDelete(doc)">
                  确认删除
                </button>
                <button type="button" class="btn btn-ghost btn-xs" @click="confirmId = null">
                  取消
                </button>
              </div>
            </div>

            <div v-if="chunkDoc?.id === doc.id" class="stack">
              <p v-if="chunksLoading" class="muted small">加载中…</p>
              <p v-else-if="!chunks.length" class="muted small">没有分块记录。</p>
              <div v-else class="kb-chunks">
                <div v-for="chunk in chunks" :key="chunk.id" class="kb-chunk">
                  <div class="kb-chunk-head">
                    <span>#{{ chunk.idx + 1 }}</span>
                    <span>
                      {{ chunk.token_count }} tokens
                      <template v-if="!chunk.has_vector">· 无向量</template>
                    </span>
                  </div>
                  <div class="kb-chunk-body">{{ chunk.text }}</div>
                </div>
              </div>
              <div class="row">
                <button type="button" class="btn btn-ghost btn-xs" @click="chunkDoc = null">
                  收起
                </button>
              </div>
            </div>
          </article>
        </section>

        <section class="card">
          <h3>搜索测试</h3>
          <p class="muted small">
            直接看召回结果，不经过助手。用来确认内容确实被检索得到。
          </p>
          <div class="row">
            <input
              v-model="searchQuery"
              class="input"
              type="text"
              placeholder="输入关键词，例如：计费规则"
              @keyup.enter="runSearch"
            />
            <button type="button" class="btn btn-primary btn-sm" :disabled="searching" @click="runSearch">
              {{ searching ? '检索中…' : '检索' }}
            </button>
          </div>

          <p v-if="searched && !hits.length" class="muted small">
            没有命中任何片段。可以换个说法，或者确认文件是否已入库。
          </p>

          <div v-for="hit in hits" :key="hit.chunk_id" class="kb-hit">
            <div class="kb-hit-head">
              <span>📄 {{ hit.source }}</span>
              <span>
                {{
                  hit.similarity === null
                    ? '仅关键词命中'
                    : `相似度 ${hit.similarity.toFixed(3)}`
                }}
                · {{ hit.tokens }} tokens
              </span>
            </div>
            <div class="kb-hit-body">{{ hit.text }}</div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>

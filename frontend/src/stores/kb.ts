import { computed, reactive } from 'vue'

import { errorMessage, kbApi } from '@/api'
import type { KbDocument, KbStats } from '@/api/types'

interface KbState {
  documents: KbDocument[]
  stats: KbStats | null
  loading: boolean
  error: string
}

export const kbState = reactive<KbState>({
  documents: [],
  stats: null,
  loading: false,
  error: '',
})

/** 正在入库中的文档。只要这个不为空就需要轮询。 */
export const activeDocuments = computed(() =>
  kbState.documents.filter((doc) => doc.status === 'pending' || doc.status === 'indexing'),
)

export const hasActive = computed(() => activeDocuments.value.length > 0)

/** 按扩展名分组，顺序固定，避免每次刷新后顺序跳动。 */
const EXT_ORDER = ['pdf', 'docx', 'md', 'txt']
const EXT_LABEL: Record<string, string> = {
  pdf: 'PDF 文档',
  docx: 'Word 文档',
  md: 'Markdown',
  txt: '纯文本',
}
const EXT_ICON: Record<string, string> = {
  pdf: '📕',
  docx: '📘',
  md: '📗',
  txt: '📄',
}

export interface KbGroup {
  ext: string
  label: string
  icon: string
  items: KbDocument[]
}

export const documentGroups = computed<KbGroup[]>(() => {
  const buckets = new Map<string, KbDocument[]>()
  for (const doc of kbState.documents) {
    const key = doc.ext || 'other'
    const bucket = buckets.get(key) ?? []
    bucket.push(doc)
    buckets.set(key, bucket)
  }

  const known = EXT_ORDER.filter((ext) => buckets.has(ext))
  const extra = [...buckets.keys()].filter((ext) => !EXT_ORDER.includes(ext))

  return [...known, ...extra].map((ext) => ({
    ext,
    label: EXT_LABEL[ext] ?? `${ext.toUpperCase()} 文件`,
    icon: EXT_ICON[ext] ?? '📎',
    items: buckets.get(ext) as KbDocument[],
  }))
})

let poller: number | null = null

/**
 * 按需轮询：只有存在进行中的文档才开着。
 * 全部进入终态就自动停，不给后端制造无谓的请求。
 */
function ensurePolling(): void {
  if (!hasActive.value) {
    stopKbPolling()
    return
  }
  if (poller !== null) return
  poller = window.setInterval(async () => {
    await refresh(true)
    if (!hasActive.value) stopKbPolling()
  }, 3000)
}

export function stopKbPolling(): void {
  if (poller !== null) {
    window.clearInterval(poller)
    poller = null
  }
}

/** 拉取文档列表；silent 用于轮询，失败不打扰用户。 */
export async function refresh(silent = false): Promise<void> {
  if (!silent) kbState.loading = true
  try {
    kbState.documents = await kbApi.list()
    kbState.error = ''
  } catch (error) {
    if (!silent) kbState.error = errorMessage(error)
  } finally {
    if (!silent) kbState.loading = false
  }
  ensurePolling()
}

export async function loadStats(): Promise<void> {
  try {
    kbState.stats = await kbApi.stats()
  } catch {
    kbState.stats = null
  }
}

/** 上传单个文件。返回错误信息，成功时返回空串。 */
export async function uploadFile(file: File): Promise<string> {
  try {
    await kbApi.upload(file)
  } catch (error) {
    return errorMessage(error)
  }
  await refresh(true)
  return ''
}

export async function removeDocument(doc: KbDocument): Promise<string> {
  try {
    await kbApi.remove(doc.id)
  } catch (error) {
    return errorMessage(error)
  }
  kbState.documents = kbState.documents.filter((item) => item.id !== doc.id)
  void loadStats()
  return ''
}

export async function retryDocument(doc: KbDocument): Promise<string> {
  try {
    await kbApi.reindex(doc.id)
  } catch (error) {
    return errorMessage(error)
  }
  await refresh(true)
  return ''
}

export function resetKb(): void {
  stopKbPolling()
  kbState.documents = []
  kbState.stats = null
  kbState.error = ''
}

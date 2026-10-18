<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { errorMessage, memoryApi } from '@/api'
import type { MemoryItem } from '@/api/types'
import { pushToast } from '@/composables/useToast'

const CATEGORY_ORDER = ['时间与日程', '常用联系人', '沟通与邮件', '我的记忆', '其他']

interface Preset {
  key: string
  label: string
  placeholder: string
  hint: string
}

const PRESETS: Preset[] = [
  {
    key: 'default_meeting_duration',
    label: '默认会议时长',
    placeholder: '例如 60',
    hint: '单位是分钟。创建日程但没说结束时间时，助手会用它补全。',
  },
  {
    key: 'work_hours_start',
    label: '工作开始时间',
    placeholder: '例如 09:00',
    hint: '用 24 小时制填写，助手安排会议时会参考。',
  },
  {
    key: 'work_hours_end',
    label: '工作结束时间',
    placeholder: '例如 18:00',
    hint: '用 24 小时制填写。',
  },
  {
    key: 'default_reminder_advance',
    label: '默认提前提醒',
    placeholder: '例如 15',
    hint: '单位是分钟，表示日程开始前多久提醒你。',
  },
  {
    key: 'email_signature',
    label: '邮件签名',
    placeholder: '例如：此致\n张三',
    hint: '助手代写邮件时会使用这段签名。',
  },
  {
    key: 'reply_tone',
    label: '邮件回复语气',
    placeholder: '例如：简洁、正式、友好',
    hint: '描述你希望助手写邮件时使用的语气。',
  },
]

const memories = ref<MemoryItem[]>([])
const loading = ref(true)
const error = ref('')
const detailKey = ref<string | null>(null)
const confirmKey = ref<string | null>(null)

const addOpen = ref(false)
const addPresetKey = ref('')
const addLabel = ref('')
const addValue = ref('')
const saving = ref(false)

const editKey = ref<string | null>(null)
const editLabel = ref('')
const editValue = ref('')

const activePreset = computed(
  () => PRESETS.find((preset) => preset.key === addPresetKey.value) ?? null,
)
const addName = computed(() => activePreset.value?.label ?? addLabel.value.trim())
const addPlaceholder = computed(() => activePreset.value?.placeholder ?? '例如：每周三下午我喜欢处理周报')
const addHint = computed(
  () =>
    activePreset.value?.hint ??
    '写一条希望助手长期记住的事实或偏好。名称由你决定，内容会跨会话生效。',
)

const groups = computed(() => {
  const map = new Map<string, MemoryItem[]>()
  for (const item of memories.value) {
    const bucket = map.get(item.category) ?? []
    bucket.push(item)
    map.set(item.category, bucket)
  }

  const known = CATEGORY_ORDER.filter((category) => map.has(category))
  const extra = [...map.keys()].filter((category) => !CATEGORY_ORDER.includes(category))
  return [...known, ...extra].map((category) => ({
    category,
    items: map.get(category) as MemoryItem[],
  }))
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

function resetAddForm(): void {
  addPresetKey.value = ''
  addLabel.value = ''
  addValue.value = ''
}

function toggleAdd(): void {
  addOpen.value = !addOpen.value
  if (!addOpen.value) resetAddForm()
}

function choosePreset(key: string): void {
  addPresetKey.value = key
  addValue.value = ''
}

function customKey(): string {
  return `custom_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`
}

function validateValue(key: string, value: string): string {
  const text = value.trim()
  if (!text) return '记忆内容不能为空'

  if (key === 'default_meeting_duration') {
    const minutes = Number(text)
    if (!Number.isInteger(minutes) || minutes <= 0 || minutes > 1440) {
      return '默认会议时长请填 1 到 1440 之间的整数（分钟）'
    }
  }
  if (key === 'default_reminder_advance') {
    const minutes = Number(text)
    if (!Number.isInteger(minutes) || minutes < 0 || minutes > 10080) {
      return '提前提醒请填 0 到 10080 之间的整数（分钟）'
    }
  }
  if (key === 'work_hours_start' || key === 'work_hours_end') {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(text)) {
      return '时间请用 24 小时制 HH:MM 格式填写，例如 09:00'
    }
  }
  return ''
}

async function saveNew(): Promise<void> {
  const key = addPresetKey.value || customKey()
  const label = addPresetKey.value ? '' : addLabel.value.trim()

  if (!addPresetKey.value && !label) {
    pushToast('请先给这条记忆起个名称', 'error', '⚠️')
    return
  }
  const invalid = validateValue(key, addValue.value)
  if (invalid) {
    pushToast(invalid, 'error', '⚠️')
    return
  }

  saving.value = true
  try {
    const item = await memoryApi.save(key, addValue.value.trim(), label)
    memories.value = [item, ...memories.value.filter((entry) => entry.key !== item.key)]
    pushToast(`已记住：${item.label}`, 'success', '🧠')
    addOpen.value = false
    resetAddForm()
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  } finally {
    saving.value = false
  }
}

function startEdit(item: MemoryItem): void {
  editKey.value = item.key
  editLabel.value = item.label
  editValue.value = item.raw_value
  detailKey.value = null
  confirmKey.value = null
}

function stopEdit(): void {
  editKey.value = null
  editLabel.value = ''
  editValue.value = ''
}

async function saveEdit(item: MemoryItem): Promise<void> {
  const invalid = validateValue(item.key, editValue.value)
  if (invalid) {
    pushToast(invalid, 'error', '⚠️')
    return
  }

  const custom = item.key.startsWith('custom_')
  const label = custom ? editLabel.value.trim() : ''
  if (custom && !label) {
    pushToast('记忆名称不能为空', 'error', '⚠️')
    return
  }

  saving.value = true
  try {
    const updated = await memoryApi.save(item.key, editValue.value.trim(), label)
    memories.value = memories.value.map((entry) =>
      entry.key === item.key ? updated : entry,
    )
    pushToast('记忆已更新', 'success', '💾')
    stopEdit()
  } catch (err) {
    pushToast(errorMessage(err), 'error', '⚠️')
  } finally {
    saving.value = false
  }
}

async function remove(item: MemoryItem): Promise<void> {
  confirmKey.value = null
  try {
    await memoryApi.remove(item.key)
    memories.value = memories.value.filter((entry) => entry.key !== item.key)
    if (editKey.value === item.key) stopEdit()
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
        <p class="muted small">
          助手会记住这里的偏好和事实，并在之后的对话中持续使用。你也可以随时新增或修改。
        </p>
      </div>
      <div class="row">
        <button type="button" class="btn btn-ghost btn-sm" @click="load">🔄 刷新</button>
        <button type="button" class="btn btn-primary btn-sm" @click="toggleAdd">
          {{ addOpen ? '收起' : '＋ 新增记忆' }}
        </button>
      </div>
    </header>

    <section v-if="addOpen" class="card memory-editor">
      <div>
        <h3>新增长期记忆</h3>
        <p class="muted small">可以添加常用偏好，也可以自由写下任何希望助手长期记住的内容。</p>
      </div>

      <div class="stack-tight">
        <span class="label">选择类型</span>
        <div class="chip-row">
          <button
            type="button"
            class="chip"
            :class="{ active: addPresetKey === '' }"
            @click="choosePreset('')"
          >
            📝 自定义
          </button>
          <button
            v-for="preset in PRESETS"
            :key="preset.key"
            type="button"
            class="chip"
            :class="{ active: addPresetKey === preset.key }"
            @click="choosePreset(preset.key)"
          >
            {{ preset.label }}
          </button>
        </div>
      </div>

      <label v-if="!activePreset" class="field">
        <span class="label">记忆名称</span>
        <input
          v-model="addLabel"
          class="input"
          maxlength="120"
          placeholder="例如：我的咖啡偏好"
        />
      </label>

      <label class="field">
        <span class="label">{{ addName || '记忆内容' }}</span>
        <textarea
          v-model="addValue"
          class="input memory-textarea"
          rows="3"
          maxlength="4000"
          :placeholder="addPlaceholder"
        />
      </label>

      <p class="muted small">{{ addHint }}</p>

      <div class="row">
        <button type="button" class="btn btn-primary" :disabled="saving" @click="saveNew">
          💾 {{ saving ? '保存中…' : '保存记忆' }}
        </button>
        <button type="button" class="btn btn-ghost" @click="toggleAdd">取消</button>
      </div>
    </section>

    <p v-if="loading" class="muted">加载中…</p>
    <p v-else-if="error" class="alert alert-error">{{ error }}</p>

    <div v-else-if="!memories.length" class="empty-state">
      <p>还没有任何长期记忆。</p>
      <p class="muted small">点击「新增记忆」，或者直接在对话里告诉助手你的偏好。</p>
    </div>

    <template v-else>
      <p class="muted small">共 {{ memories.length }} 条记忆</p>

      <section v-for="group in groups" :key="group.category" class="memory-group">
        <h2 class="group-title">{{ group.category }}（{{ group.items.length }}）</h2>

        <article v-for="item in group.items" :key="item.key" class="card memory-card">
          <template v-if="editKey === item.key">
            <div class="memory-main">
              <h3>✏️ 编辑「{{ item.label }}」</h3>
            </div>
            <label v-if="item.key.startsWith('custom_')" class="field">
              <span class="label">记忆名称</span>
              <input v-model="editLabel" class="input" maxlength="120" />
            </label>
            <label class="field">
              <span class="label">记忆内容</span>
              <textarea
                v-model="editValue"
                class="input memory-textarea"
                rows="3"
                maxlength="4000"
              />
            </label>
            <div class="row">
              <button
                type="button"
                class="btn btn-primary btn-sm"
                :disabled="saving"
                @click="saveEdit(item)"
              >
                💾 {{ saving ? '保存中…' : '保存' }}
              </button>
              <button type="button" class="btn btn-ghost btn-sm" @click="stopEdit">取消</button>
            </div>
          </template>

          <template v-else>
            <div class="memory-main">
              <h3>{{ item.icon }} {{ item.label }}</h3>
              <p class="memory-value">{{ item.display_value }}</p>
            </div>

            <div class="memory-actions">
              <button type="button" class="btn btn-ghost btn-xs" @click="startEdit(item)">
                ✏️ 编辑
              </button>
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
              <p class="small">确定删除这条记忆？助手之后就不会再记住它了。</p>
              <div class="confirm-actions">
                <button type="button" class="btn btn-danger btn-xs" @click="remove(item)">
                  确认删除
                </button>
                <button type="button" class="btn btn-ghost btn-xs" @click="confirmKey = null">
                  取消
                </button>
              </div>
            </div>
          </template>
        </article>
      </section>
    </template>
  </div>
</template>

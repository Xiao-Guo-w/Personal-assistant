<script setup lang="ts">
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { computed } from 'vue'

const props = defineProps<{ content: string }>()

marked.setOptions({ breaks: true, gfm: true })

const html = computed(() => DOMPurify.sanitize(marked.parse(props.content ?? '') as string))
</script>

<template>
  <!-- 内容已由 DOMPurify 过滤 -->
  <div class="markdown" v-html="html" />
</template>

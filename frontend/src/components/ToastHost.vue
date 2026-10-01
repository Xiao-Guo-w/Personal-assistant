<script setup lang="ts">
import { dismissToast, toasts } from '@/composables/useToast'
</script>

<template>
  <div class="toast-host">
    <TransitionGroup name="toast">
      <button
        v-for="item in toasts"
        :key="item.id"
        type="button"
        class="toast"
        :class="`toast-${item.tone}`"
        @click="dismissToast(item.id)"
      >
        <span v-if="item.icon" class="toast-icon">{{ item.icon }}</span>
        <span>{{ item.text }}</span>
      </button>
    </TransitionGroup>
  </div>
</template>

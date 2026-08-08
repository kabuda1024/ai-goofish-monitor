<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Badge } from '@/components/ui/badge'
import type { TaskGenerationJob, TaskGenerationStep } from '@/types/task.d.ts'

const props = defineProps<{
  job: TaskGenerationJob
}>()
const { t } = useI18n()

const statusMeta = computed(() => {
  if (props.job.status === 'completed') {
    return { label: t('tasks.generation.status.completed'), variant: 'default' as const }
  }
  if (props.job.status === 'failed') {
    return { label: t('tasks.generation.status.failed'), variant: 'destructive' as const }
  }
  if (props.job.status === 'running') {
    return { label: t('tasks.generation.status.running'), variant: 'secondary' as const }
  }
  return { label: t('tasks.generation.status.queued'), variant: 'outline' as const }
})

// 从生成的任务里提取 AI 建议的关键词候选(如果有)
const searchKeywords = computed<string[]>(() => {
  const options = props.job.task?.platform_options as
    | { search_keywords?: unknown }
    | undefined
  const raw = options?.search_keywords
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => (typeof item === 'string' ? item.trim() : ''))
    .filter((item) => item.length > 0)
})

const platformLabel = computed(() => {
  const platform = props.job.task?.platform || 'xianyu'
  if (platform === 'mercari') return 'Mercari'
  if (platform === 'hoyoyo') return 'Hoyoyo'
  return '闲鱼'
})

function resolveStepDotClass(step: TaskGenerationStep) {
  if (step.status === 'completed') return 'border-emerald-500 bg-emerald-500'
  if (step.status === 'running') return 'border-amber-500 bg-amber-500 shadow-[0_0_0_4px_rgba(245,158,11,0.18)]'
  if (step.status === 'failed') return 'border-red-500 bg-red-500'
  return 'border-slate-300 bg-white'
}

function resolveStepTextClass(step: TaskGenerationStep) {
  if (step.status === 'completed') return 'text-slate-700'
  if (step.status === 'running') return 'text-slate-900'
  if (step.status === 'failed') return 'text-red-600'
  return 'text-slate-400'
}
</script>

<template>
  <section class="rounded-2xl border border-slate-200 bg-slate-50/80 p-4 shadow-sm">
    <div class="flex items-start justify-between gap-4">
      <div class="space-y-1">
        <p class="text-sm font-semibold text-slate-900">
          {{ job.task_name }}
        </p>
        <p class="text-sm text-slate-600">
          {{ job.message }}
        </p>
      </div>
      <Badge :variant="statusMeta.variant">
        {{ statusMeta.label }}
      </Badge>
    </div>

    <div class="mt-4 grid gap-3">
      <div
        v-for="step in job.steps"
        :key="step.key"
        class="flex items-start gap-3 rounded-xl border border-white/70 bg-white px-3 py-2"
      >
        <span
          class="mt-1 h-3 w-3 shrink-0 rounded-full border-2 transition-colors"
          :class="resolveStepDotClass(step)"
        />
        <div class="min-w-0 space-y-1">
          <p class="text-sm font-medium" :class="resolveStepTextClass(step)">
            {{ step.label }}
          </p>
          <p
            v-if="step.message"
            class="text-xs"
            :class="step.status === 'failed' ? 'text-red-500' : 'text-slate-500'"
          >
            {{ step.message }}
          </p>
        </div>
      </div>
    </div>

    <!-- AI 生成的搜索关键词候选(仅在任务生成完成后展示) -->
    <div
      v-if="job.status === 'completed' && searchKeywords.length > 0"
      class="mt-4 rounded-xl border border-blue-200 bg-blue-50/60 px-4 py-3"
    >
      <div class="flex items-center justify-between gap-2 mb-2">
        <p class="text-sm font-semibold text-blue-900">
          🔍 {{ platformLabel }} 搜索关键词候选
        </p>
        <Badge variant="outline" class="text-xs">
          {{ searchKeywords.length }} 个
        </Badge>
      </div>
      <p class="text-xs text-blue-700/80 mb-2">
        本次运行将依次搜索以下关键词(去重后合并结果)。如需调整,可到任务详情页编辑
        <code class="bg-white px-1 rounded">platform_options.search_keywords</code>。
      </p>
      <div class="flex flex-wrap gap-1.5">
        <span
          v-for="kw in searchKeywords"
          :key="kw"
          class="inline-flex items-center px-2 py-0.5 rounded-md bg-white text-xs font-medium text-blue-900 border border-blue-200"
        >
          {{ kw }}
        </span>
      </div>
    </div>

    <p
      v-if="job.error"
      class="mt-4 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-600"
    >
      {{ job.error }}
    </p>
  </section>
</template>

<template>
  <section class="page" data-module="plan">
    <header class="page-head">
      <div>
        <h2>{{ detail ? '表优化执行计划详情' : '表优化执行计划' }}</h2>
        <p class="page-desc">
          读取表优化任务与容量统计，按资源配置总预算编排批次；同表只保留一个活动任务，
          执行后把优化前后的文件与容量以及实际效果写回表统计信息与预算核对清单。
        </p>
      </div>
      <div class="page-actions">
        <template v-if="!detail">
          <button class="btn primary" type="button" :disabled="busy" @click="generatePlan">
            生成执行计划
          </button>
          <button class="btn" type="button" @click="openConfig">预算配置</button>
          <button class="btn" type="button" @click="goList">刷新计划列表</button>
        </template>
        <template v-else>
          <button class="btn" type="button" @click="backToList">返回计划列表</button>
          <button class="btn" type="button" @click="downloadDetail">导出计划明细</button>
        </template>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in summaryCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text">{{ notice }}</p>

    <!-- 列表页：候选视图 + 计划列表 + 预算核对清单 -->
    <template v-if="!detail">
      <section class="panel">
        <div class="panel-head">
          <h3>执行计划候选</h3>
          <span class="panel-meta">
            预算 {{ config.资源总预算 || '—' }}（行组上限 {{ config.行组大小上限 || '—' }}，默认按{{ config.默认排序 || '收益比例' }}排序）
          </span>
        </div>
        <form class="filter-bar" @submit.prevent="loadCandidates">
          <label class="filter-item">
            <span>排序方式</span>
            <select v-model="sortBy" @change="sortByTouched = true">
              <option v-for="option in sortOptions" :key="option" :value="option">{{ option }}</option>
            </select>
          </label>
          <button class="btn" type="submit">按此排序</button>
          <span v-if="tableFilter" class="panel-meta">
            当前只看表 {{ tableFilter }}
            <button class="link" type="button" @click="clearTableFilter">查看全部候选</button>
          </span>
          <span class="panel-meta">
            可编排预算：{{ budgetText(预算.可编排预算) }} · 已预留 {{ budgetText(预算.已预留) }}
          </span>
        </form>
        <table class="data-table">
          <thead>
            <tr>
              <th>表名称</th><th>任务编号</th><th>优化类型</th>
              <th>优化前容量</th><th>优化前文件数</th>
              <th>优化后容量</th><th>优化后文件数</th>
              <th>预算占用</th><th>预计收益</th><th>收益比例</th><th>编排结论</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in visibleCandidates" :key="`${row.表名称}-${row.任务编号}`">
              <td>
                <RouterLink v-if="row.可编排" class="link" :to="{ path: '/plan', query: { table: row.表名称 } }">
                  {{ row.表名称 }}
                </RouterLink>
                <span v-else>{{ row.表名称 }}</span>
              </td>
              <td>{{ row.任务编号 ?? '—' }}</td>
              <td>{{ row.优化类型 ?? '—' }}</td>
              <td>{{ row.优化前容量 ?? '—' }}</td>
              <td>{{ row.优化前文件数 ?? '—' }}</td>
              <td>{{ row.优化后容量 ?? '—' }}</td>
              <td>{{ row.优化后文件数 ?? '—' }}</td>
              <td>{{ row.预算占用 ?? '—' }}</td>
              <td>{{ row.预计收益 ?? '—' }}</td>
              <td>{{ row.收益比例 ?? '—' }}</td>
              <td>
                <span v-if="row.可编排">{{ row.已预留 ? '已被其他计划预留' : '可编排' }}</span>
                <span v-else class="muted">{{ row.跳过原因 }}</span>
              </td>
            </tr>
            <tr v-if="!visibleCandidates.length">
              <td colspan="11" class="empty-state">
                {{ tableFilter ? `表 ${tableFilter} 暂无候选` : '暂无候选，请先在表优化管理登记优化任务并采集容量统计' }}
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="重复任务.length" class="panel-meta">
          同表去重：{{ 重复任务.map((item) => `${item.表名称} 保留 ${item.保留任务}、跳过 ${item.跳过任务}`).join('；') }}
        </p>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>执行计划列表</h3>
          <span class="panel-meta">共 {{ planTotal }} 份计划</span>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>计划编号</th><th>版本</th><th>状态</th><th>排序</th>
              <th>资源总预算</th><th>已用预算</th><th>剩余预算</th>
              <th>入选/跳过</th><th>创建时间</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="plan in plans" :key="String(plan.id)">
              <td>
                <RouterLink class="link" :to="`/plan/${plan.id}`">{{ plan.计划编号 }}</RouterLink>
              </td>
              <td>v{{ plan.版本 }}</td>
              <td>{{ plan.状态 }}</td>
              <td>{{ plan.排序 }}</td>
              <td>{{ plan.资源总预算 }}</td>
              <td>{{ plan.已用预算 }}</td>
              <td>{{ plan.剩余预算 }}</td>
              <td>{{ plan.入选数量 }} / {{ plan.跳过数量 }}</td>
              <td>{{ plan.创建时间 }}</td>
              <td class="row-actions">
                <button class="link" type="button" @click="openPlan(plan)">计划详情</button>
              </td>
            </tr>
            <tr v-if="!plans.length">
              <td colspan="10" class="empty-state">暂无执行计划，可点击「生成执行计划」按当前预算编排</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>预算核对清单</h3>
          <span class="panel-meta">核对结果由执行计划同步，计划与清单保持同版本</span>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>核对编号</th><th>计划编号</th><th>版本</th><th>表名称</th><th>任务编号</th>
              <th>预算占用</th><th>累计预算</th><th>预算结论</th>
              <th>优化前容量</th><th>优化后容量</th><th>实际收益</th><th>核对状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in reconcilePreview" :key="String(row.id)">
              <td>{{ row.核对编号 }}</td>
              <td>
                <RouterLink class="link" :to="`/plan/${row.计划id}`">{{ row.计划编号 }}</RouterLink>
              </td>
              <td>v{{ row.计划版本 }}</td>
              <td>{{ row.表名称 }}</td>
              <td>{{ row.任务编号 }}</td>
              <td>{{ row.预算占用 }}</td>
              <td>{{ row.累计预算 }}</td>
              <td>{{ row.预算结论 }}</td>
              <td>{{ row.优化前容量 }}</td>
              <td>{{ row.优化后容量 }}</td>
              <td>{{ row.实际收益 }}</td>
              <td>{{ row.核对状态 }}</td>
            </tr>
            <tr v-if="!reconcilePreview.length">
              <td colspan="12" class="empty-state">预算核对清单为空，保存执行计划后会自动按版本生成核对项</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>

    <!-- 详情页：预算、优化前后容量与实际效果 -->
    <template v-else>
      <section class="panel">
        <div class="panel-head">
          <h3>{{ detail.计划编号 }} · v{{ detail.版本 }} · {{ detail.状态 }}</h3>
          <span class="panel-meta">
            资源总预算 {{ detail.资源总预算 }} · 已用 {{ detail.已用预算 }} · 剩余 {{ detail.剩余预算 }}
            · 行组上限 {{ detail.行组大小上限 }}
          </span>
        </div>
        <div class="action-bar">
          <button class="btn primary" type="button" :disabled="busy" @click="runPlanAction('执行计划')">执行计划</button>
          <button class="btn" type="button" :disabled="busy" @click="runPlanAction('重新编排', '收益比例')">按收益比例重新编排</button>
          <button class="btn" type="button" :disabled="busy" @click="runPlanAction('同步核对')">同步核对清单</button>
          <button class="btn" type="button" :disabled="busy" @click="restorePlan">记录恢复结论</button>
          <button class="btn ghost" type="button" :disabled="busy" @click="runPlanAction('归档计划')">归档计划</button>
          <button class="btn" type="button" :disabled="busy" @click="detectConflicts">检查版本冲突</button>
        </div>
        <p class="panel-meta">
          版本核对：计划 v{{ detail.版本 }}，核对清单
          {{ (detail.版本核对?.核对清单版本 || []).map((item: number) => `v${item}`).join('、') || '—' }}
          {{ detail.版本核对?.一致 ? '· 同版本' : '· 版本不一致，请先同步核对清单' }}
        </p>
      </section>

      <section v-if="待处理冲突.length" class="panel">
        <div class="panel-head">
          <h3>版本冲突</h3>
          <span class="panel-meta">新到达的高收益任务与已预留预算冲突，处理前不能执行</span>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>冲突编号</th><th>表名称</th><th>原任务</th><th>原收益比例</th>
              <th>新任务</th><th>新收益比例</th><th>新任务预算</th><th>说明</th><th>处理</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="conflict in 待处理冲突" :key="String(conflict.id)">
              <td>{{ conflict.冲突编号 }}</td>
              <td>{{ conflict.表名称 }}</td>
              <td>{{ conflict.原任务编号 }}</td>
              <td>{{ conflict.原收益比例 }}</td>
              <td>{{ conflict.新任务编号 }}</td>
              <td>{{ conflict.新收益比例 }}</td>
              <td>{{ conflict.新任务预算占用 }}</td>
              <td>{{ conflict.说明 }}</td>
              <td class="row-actions">
                <button class="link" type="button" :disabled="busy" @click="resolve(conflict, '保留原任务')">保留原任务</button>
                <button class="link" type="button" :disabled="busy" @click="resolve(conflict, '改排新任务')">改排新任务</button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>计划明细：预算与优化前后容量</h3>
          <span class="panel-meta">入选 {{ detail.入选数量 }} 张表 · 跳过 {{ detail.跳过数量 }} 个候选</span>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>表名称</th><th>任务编号</th><th>优化类型</th>
              <th>预算占用</th><th>预计收益</th><th>收益比例</th>
              <th>优化前容量</th><th>优化前文件数</th>
              <th>优化后容量</th><th>优化后文件数</th>
              <th>实际优化后容量</th><th>实际优化后文件数</th>
              <th>实际收益</th><th>执行结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in detail.entries" :key="String(entry.任务编号)">
              <td>{{ entry.表名称 }}</td>
              <td>{{ entry.任务编号 }}</td>
              <td>{{ entry.优化类型 }}</td>
              <td>{{ entry.预算占用 }}</td>
              <td>{{ entry.预计收益 }}</td>
              <td>{{ entry.收益比例 }}</td>
              <td>{{ entry.优化前容量 }}</td>
              <td>{{ entry.优化前文件数 }}</td>
              <td>{{ entry.优化后容量 }}</td>
              <td>{{ entry.优化后文件数 }}</td>
              <td>{{ entry.实际优化后容量 || '—' }}</td>
              <td>{{ entry.实际优化后文件数 || '—' }}</td>
              <td>{{ entry.实际收益 || '—' }}</td>
              <td>{{ entry.执行结果 }}</td>
            </tr>
            <tr v-if="!detail.entries.length">
              <td colspan="14" class="empty-state">该批次没有入选的表</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>未入选候选</h3>
          <span class="panel-meta">预算不足时会跳过当前候选、继续比对剩余候选</span>
        </div>
        <table class="data-table">
          <thead>
            <tr><th>表名称</th><th>任务编号</th><th>跳过原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in detail.skipped" :key="`${row.表名称}-${row.任务编号}`">
              <td>{{ row.表名称 }}</td>
              <td>{{ row.任务编号 }}</td>
              <td>{{ row.原因 }}</td>
            </tr>
            <tr v-if="!detail.skipped.length">
              <td colspan="3" class="empty-state">候选已全部入选</td>
            </tr>
          </tbody>
        </table>
        <p v-if="(detail.同表重复任务 || []).length" class="panel-meta">
          同表去重：{{ detail.同表重复任务.map((item: any) => `${item.表名称} 保留 ${item.保留任务}、跳过 ${item.跳过任务}`).join('；') }}
        </p>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>预算核对清单 · v{{ detail.版本 }}</h3>
          <span class="panel-meta">本清单与计划同版本；恢复结论也会记在这里</span>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>核对编号</th><th>表名称</th><th>任务编号</th><th>预算占用</th>
              <th>累计预算</th><th>预算结论</th>
              <th>优化前容量</th><th>优化前文件数</th>
              <th>优化后容量</th><th>优化后文件数</th>
              <th>预计收益</th><th>实际收益</th><th>核对状态</th><th>核对说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in detail.预算核对清单" :key="String(row.id)">
              <td>{{ row.核对编号 }}</td>
              <td>{{ row.表名称 }}</td>
              <td>{{ row.任务编号 }}</td>
              <td>{{ row.预算占用 }}</td>
              <td>{{ row.累计预算 }}</td>
              <td>{{ row.预算结论 }}</td>
              <td>{{ row.优化前容量 }}</td>
              <td>{{ row.优化前文件数 }}</td>
              <td>{{ row.优化后容量 }}</td>
              <td>{{ row.优化后文件数 }}</td>
              <td>{{ row.预计收益 }}</td>
              <td>{{ row.实际收益 }}</td>
              <td>{{ row.核对状态 }}</td>
              <td>{{ row.核对说明 }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="detail.恢复结论" class="panel-meta">恢复结论：{{ detail.恢复结论 }}（{{ detail.恢复时间 }}）</p>
        <p class="panel-meta">
          统计报告：{{ (detail.统计报告编号 || []).join('、') || '执行后写入表统计信息' }}
        </p>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>版本历史</h3>
          <span class="panel-meta">版本冲突处理与重新编排都会推进版本</span>
        </div>
        <table class="data-table">
          <thead>
            <tr><th>版本</th><th>操作</th><th>状态</th><th>排序</th><th>已用预算</th><th>入选/跳过</th><th>任务快照</th><th>时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in detail.版本历史" :key="String(row.id)">
              <td>v{{ row.版本 }}</td>
              <td>{{ row.操作 }}</td>
              <td>{{ row.状态 }}</td>
              <td>{{ row.排序 }}</td>
              <td>{{ row.已用预算 }}</td>
              <td>{{ row.入选数量 }} / {{ row.跳过数量 }}</td>
              <td>{{ (row.任务快照 || []).join('、') }}</td>
              <td>{{ row.时间 }}</td>
            </tr>
            <tr v-if="!(detail.版本历史 || []).length">
              <td colspan="8" class="empty-state">暂无版本记录</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { fetchJson, request } from '@/api/client'

type Candidate = Record<string, any>
type PlanSummary = Record<string, any>
type PlanDetail = Record<string, any>
type ReconcileRow = Record<string, any>

const route = useRoute()
const router = useRouter()

const sortOptions = ['收益比例', '预计收益', '预算占用', '优化前容量', '任务编号', '表名称']
const sortBy = ref('收益比例')
const sortByTouched = ref(false)
const busy = ref(false)
const errorMessage = ref('')
const notice = ref('')

const config = ref<Record<string, any>>({})
const candidateItems = ref<Candidate[]>([])
const 重复任务 = ref<Record<string, any>[]>([])
const 预算 = reactive<Record<string, any>>({})
const plans = ref<PlanSummary[]>([])
const planTotal = ref(0)
const reconcilePreview = ref<ReconcileRow[]>([])
const detail = ref<PlanDetail | null>(null)
const tableFilter = ref('')

const 待处理冲突 = computed<Record<string, any>[]>(() => detail.value?.待处理冲突 ?? [])

const visibleCandidates = computed<Candidate[]>(() => {
  if (!tableFilter.value) return candidateItems.value
  return candidateItems.value.filter((item) => String(item.表名称) === tableFilter.value)
})

const summaryCards = computed(() => {
  if (detail.value) {
    return [
      { label: '计划版本', value: `v${detail.value.版本}` },
      { label: '已用预算', value: detail.value.已用预算 },
      { label: '剩余预算', value: detail.value.剩余预算 },
      { label: '入选表数', value: detail.value.入选数量 },
      { label: '核对项', value: (detail.value.预算核对清单 || []).length },
    ]
  }
  return [
    { label: '资源总预算', value: config.value.资源总预算 || '—' },
    { label: '行组大小上限', value: config.value.行组大小上限 || '—' },
    { label: '候选表数', value: candidateItems.value.length },
    { label: '执行计划', value: planTotal.value },
    { label: '核对项', value: reconcilePreview.value.length },
  ]
})

function budgetText(value: unknown) {
  return typeof value === 'string' && value ? value : '—'
}

function planId(): number | null {
  const raw = route.params.id
  if (raw === undefined || raw === null || raw === '') return null
  const parsed = Number(raw)
  return Number.isFinite(parsed) ? parsed : null
}

function openPlan(plan: PlanSummary) {
  void router.push(`/plan/${plan.id}`)
}

function goList() {
  if (detail.value) {
    void router.push('/plan')
    return
  }
  void loadCandidates()
  void loadPlans()
  void loadReconcile()
}

function backToList() {
  void router.push('/plan')
}

function clearTableFilter() {
  tableFilter.value = ''
  void router.replace('/plan')
}

function downloadDetail() {
  if (!detail.value) return
  const blob = new Blob([JSON.stringify(detail.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `${detail.value.计划编号}-v${detail.value.版本}.json`
  link.click()
  URL.revokeObjectURL(url)
}

function openConfig() {
  const budget = window.prompt('资源总预算（例如 800 GB）', config.value.资源总预算 || '800 GB')
  if (budget === null) return
  const group = window.prompt('行组大小上限（例如 512 MB）', config.value.行组大小上限 || '512 MB')
  if (group === null) return
  void saveConfig(budget, group)
}

async function saveConfig(budget: string, group: string) {
  busy.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    const response = await request('/api/plan/config', {
      method: 'POST',
      body: JSON.stringify({ values: { 资源总预算: budget, 行组大小上限: group, 默认排序: config.value.默认排序 || '收益比例' } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || payload.detail || '预算配置未生效')
    }
    notice.value = payload.message
    await loadConfig()
    await loadCandidates()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预算配置更新失败'
  } finally {
    busy.value = false
  }
}

async function loadConfig() {
  try {
    config.value = await fetchJson<Record<string, any>>('/api/plan/config')
    if (!sortByTouched.value && typeof config.value.默认排序 === 'string' && config.value.默认排序) {
      sortBy.value = config.value.默认排序
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预算配置读取失败'
  }
}

async function loadCandidates() {
  try {
    const payload = await fetchJson<Record<string, any>>(
      `/api/plan/candidates?sort_by=${encodeURIComponent(sortBy.value)}`,
    )
    candidateItems.value = payload.items ?? []
    重复任务.value = payload.同表重复任务 ?? []
    Object.assign(预算, payload.预算 ?? {})
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '执行计划候选读取失败'
  }
}

async function loadPlans() {
  try {
    const payload = await fetchJson<Record<string, any>>('/api/plan/plans?size=50')
    plans.value = payload.items ?? []
    planTotal.value = payload.total ?? plans.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '执行计划列表读取失败'
  }
}

async function loadReconcile() {
  try {
    const payload = await fetchJson<Record<string, any>>('/api/plan/reconcile')
    reconcilePreview.value = (payload.items ?? []).slice(0, 50)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预算核对清单读取失败'
  }
}

async function loadDetail(id: number) {
  try {
    detail.value = await fetchJson<PlanDetail>(`/api/plan/plans/${id}`)
  } catch (error) {
    detail.value = null
    errorMessage.value = error instanceof Error ? error.message : '执行计划详情读取失败'
  }
}

async function generatePlan() {
  busy.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    const response = await request('/api/plan/plans', {
      method: 'POST',
      body: JSON.stringify({ values: { 排序: sortBy.value } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || payload.detail || '执行计划未生成')
    }
    notice.value = payload.message
    // 保存后回看优化列表，再转到计划详情。
    await loadPlans()
    await loadReconcile()
    await loadCandidates()
    const id = payload.entry?.id
    if (id) {
      await router.push(`/plan/${id}`)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成执行计划失败'
  } finally {
    busy.value = false
  }
}

async function planAction(action: string, remark?: string) {
  const current = detail.value
  if (!current) return
  busy.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    const response = await request(`/api/plan/plans/${current.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, remark } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || payload.detail || '执行计划动作未生效')
    }
    notice.value = payload.message
    await loadDetail(current.id)
    await loadPlans()
    await loadReconcile()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '执行计划动作失败'
  } finally {
    busy.value = false
  }
}

function runPlanAction(action: string, remark?: string) {
  void planAction(action, remark)
}

function restorePlan() {
  const conclusion = window.prompt(
    '恢复结论（会写入预算核对清单）',
    '恢复完成，优化效果保留在表统计信息中，预算核对清单已记录恢复结论',
  )
  if (conclusion === null) return
  void planAction('恢复', conclusion)
}

async function detectConflicts() {
  const current = detail.value
  if (!current) return
  busy.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    const response = await request('/api/plan/conflicts/detect', {
      method: 'POST',
      body: JSON.stringify({ values: { plan_id: current.id } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || payload.detail || '版本冲突检查失败')
    }
    notice.value = payload.message
    await loadDetail(current.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本冲突检查失败'
  } finally {
    busy.value = false
  }
}

async function resolve(conflict: Record<string, any>, action: string) {
  const current = detail.value
  if (!current) return
  busy.value = true
  errorMessage.value = ''
  notice.value = ''
  try {
    const response = await request(`/api/plan/conflicts/${conflict.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || payload.detail || '版本冲突处理未生效')
    }
    notice.value = payload.message
    await loadDetail(current.id)
    await loadPlans()
    await loadReconcile()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本冲突处理失败'
  } finally {
    busy.value = false
  }
}

async function refreshByRoute() {
  errorMessage.value = ''
  notice.value = ''
  await loadConfig()
  const id = planId()
  if (id !== null) {
    detail.value = null
    await loadDetail(id)
    return
  }
  detail.value = null
  tableFilter.value = String(route.query.table ?? '')
  if (tableFilter.value) {
    notice.value = `候选表 ${tableFilter.value} 已定位，可在下方候选里核对预算与收益`
  }
  await loadCandidates()
  await loadPlans()
  await loadReconcile()
}

watch(
  () => route.fullPath,
  () => {
    void refreshByRoute()
  },
)

onMounted(refreshByRoute)
</script>

<template>
  <section class="page" data-module="backfill_recovery">
    <header class="page-head">
      <div>
        <h2>补数恢复清单</h2>
        <p class="page-desc">保留补数任务的已完成偏移与待补区间，中断后按清单继续补数，合并清理据此核对可合并范围。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="generate-manifest" type="button" @click="generate">生成恢复清单</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出恢复清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="applyFilter">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter" data-testid="filter-status">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>关键字</span>
        <input v-model="keyword" data-testid="keyword" placeholder="按清单/任务/表/批次检索" />
      </label>
      <label class="filter-item">
        <span>每页条数</span>
        <input v-model.number="size" type="number" min="1" max="200" data-testid="page-size" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :data-testid="`row-${row.id}`"
          :data-status="String(row.status)"
          :data-offset="String(row['已完成偏移'])"
          :data-pending="String(row['待补区间'])"
          @click="openDetail(row)"
        >
          <td v-for="column in columns" :key="column">{{ display(row, column) }}</td>
          <td class="row-actions">
            <button
              v-for="action in rowActions(row)"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click.stop="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无补数恢复清单，可先从补数任务生成</td>
        </tr>
      </tbody>
    </table>

    <div class="pager" data-testid="pager">
      <button class="btn" type="button" data-testid="prev-page" :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
      <span>第 {{ page }} / {{ totalPages }} 页</span>
      <button class="btn" type="button" data-testid="next-page" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</button>
      <span>共 {{ total }} 条补数恢复记录</span>
    </div>

    <aside v-if="detail" class="detail-panel" data-testid="recovery-detail">
      <h3>{{ detail['清单编号'] }} · {{ detail['任务编号'] }}</h3>
      <dl>
        <dt>已完成偏移</dt><dd data-testid="detail-offset">{{ detail['已完成偏移'] }}</dd>
        <dt>待补区间</dt><dd data-testid="detail-pending">{{ detail['待补区间'] || '无' }}</dd>
        <dt>恢复起点</dt><dd>{{ detail['恢复起点'] }}（{{ detail['恢复策略'] }}）</dd>
        <dt>来源批次</dt><dd>{{ detail['来源批次'] }}</dd>
        <dt>冲突判定</dt><dd>{{ detail['冲突判定'] }}</dd>
        <dt>数据版本</dt><dd>{{ detail['数据版本'] }}</dd>
      </dl>
      <p v-if="detail['重试结果']" class="detail-note">
        重试结果：应用 {{ detail['重试结果']['应用条数'] }} 条，跳过重复 {{ detail['重试结果']['跳过重复条数'] }} 条，
        跳过迟到 {{ detail['重试结果']['跳过迟到条数'] }} 条。
      </p>
      <button class="btn" type="button" data-testid="close-detail" @click="detail = null">关闭</button>
    </aside>

    <footer class="page-foot">
      <span>{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/backfill_recovery'
const columns = ['清单编号', '任务编号', '目标表名', '来源批次', '已完成偏移', '待补区间', '恢复起点', '恢复策略', '冲突判定', '数据版本']
const statuses = ['待恢复', '已完成']
const filterFields = ['清单编号', '任务编号', '目标表名', '来源批次']

const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const keyword = ref('')
const statusFilter = ref('')
const filters = ref<Record<string, string>>({})
const detail = ref<Row | null>(null)
const message = ref('')
const errorMessage = ref('')

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / size.value)))
const stats = computed(() => [
  { label: '恢复清单总数', value: total.value },
  { label: '待恢复清单', value: rows.value.filter((row) => row.status === '待恢复').length },
  { label: '待补条数', value: rows.value.reduce((sum, row) => sum + Number(row['剩余条数'] || 0), 0) },
])

function display(row: Row, column: string) {
  const value = row[column]
  return value === null || value === undefined || value === '' ? '—' : value
}

function rowActions(row: Row) {
  if (row['冲突判定'] === '待裁决') return ['续跑', '重放']
  if (row['剩余条数'] > 0) return ['重试恢复']
  return []
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => { if (value) query.set(key, value) })
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  query.set('page', String(page.value))
  query.set('size', String(size.value))
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) throw new Error('补数恢复清单读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    page.value = payload.page ?? page.value
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补数恢复清单读取失败'
  }
}

function applyFilter() {
  page.value = 1
  void reload()
}

function resetFilters() {
  filters.value = {}
  keyword.value = ''
  statusFilter.value = ''
  size.value = 20
  applyFilter()
}

function goPage(next: number) {
  page.value = Math.min(Math.max(1, next), totalPages.value)
  void reload()
}

async function openDetail(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) throw new Error('清单明细读取失败')
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清单明细读取失败'
  }
}

async function generate() {
  errorMessage.value = ''
  try {
    const response = await request('/api/backfill_stream/orders/recovery', { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '生成失败')
    message.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST', body: JSON.stringify({ action: action === '重试恢复' ? '重试恢复' : action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '操作未生效')
    message.value = payload.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补数恢复操作失败'
  }
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => { if (value) query.set(key, value) })
  if (keyword.value) query.set('keyword', keyword.value)
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}/export?${query.toString()}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'backfill_recovery.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.pager { display: flex; align-items: center; gap: 10px; margin-top: 8px; font-size: 13px; color: var(--muted); }
.detail-panel { margin-top: 12px; padding: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; }
.detail-panel dl { display: grid; grid-template-columns: 120px 1fr; gap: 4px 12px; margin: 8px 0; }
.detail-panel dt { color: var(--muted); font-size: 12px; }
.detail-panel dd { margin: 0; font-size: 13px; }
.detail-note { font-size: 12px; color: var(--muted); }
</style>

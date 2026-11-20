<template>
  <section class="page" data-module="backfill_stream">
    <header class="page-head">
      <div><h2>流式补数</h2><p class="page-desc">维护流式数据的历史偏移区间回溯与批量重放补数，为流式补数提供可验证的数据工程流程。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记补数任务</button>
        <button class="btn" type="button" data-testid="generate-recovery" @click="generateRecovery">生成补数恢复清单</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <section class="recovery-progress" data-testid="order-progress">
      <h3>订单表补数进度</h3>
      <table class="data-table">
        <thead><tr><th>任务编号</th><th>状态</th><th>已完成偏移</th><th>待补区间</th><th>来源批次</th><th>恢复清单</th></tr></thead>
        <tbody>
          <tr v-for="task in orderTasks" :key="task['任务编号']" :data-testid="`progress-${task['任务编号']}`" :data-status="task.status">
            <td>{{ task['任务编号'] }}</td>
            <td>{{ task.status }}</td>
            <td>{{ task['已完成偏移'] }}</td>
            <td>{{ task['待补区间'] || '无' }}</td>
            <td>{{ task['来源批次'] || '—' }}</td>
            <td>{{ task['恢复清单'] ? `${task['恢复清单']}（${task['恢复清单状态']}）` : '未生成' }}</td>
          </tr>
          <tr v-if="!orderTasks.length"><td colspan="6" class="empty-state">暂无订单表补数数据任务</td></tr>
        </tbody>
      </table>
      <p v-if="recoveryMessage" class="progress-note" data-testid="recovery-message">{{ recoveryMessage }}</p>
    </section>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
      <div style="margin-top:4px;">
        已完成偏移：{{ selectedItem['已完成偏移'] ?? '—' }}；待补区间：{{ selectedItem['待补区间'] || '无' }}；来源批次：{{ selectedItem['来源批次'] || '—' }}
      </div>
    </div>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记补数任务</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/backfill_stream'
const columns = ["任务编号", "目标表名", "起始偏移", "截止偏移", "重放速率", "并行度", "覆盖策略", "结果校验"]
const actions = ["启动补数", "确认完成", "取消补数"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const orderTasks = ref<any[]>([])
const recoveryMessage = ref('')
async function loadOrderTasks() {
  try {
    const res = await request(`${ENDPOINT}/orders/tasks?size=200`)
    if (!res.ok) return
    const data = await res.json()
    orderTasks.value = data.items || []
  } catch (e) { /* 进度面板失败不影响时间线 */ }
}
async function generateRecovery() {
  errorMessage.value = ''; recoveryMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/orders/recovery`, { method: 'POST' })
    const payload = await res.json()
    if (!res.ok || !payload.ok) throw new Error(payload.message || '生成恢复清单失败')
    recoveryMessage.value = payload.message
    await loadOrderTasks()
  } catch (e: any) { errorMessage.value = e.message }
}
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter(); await loadOrderTasks() } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>

<style scoped>
.recovery-progress { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.recovery-progress h3 { font-size: 14px; margin: 0 0 8px; }
.progress-note { font-size: 12px; color: var(--muted); margin: 8px 0 0; }
.timeline-bar.status-已中断 { background: #f97316; }
.timeline-bar.status-已取消 { background: #94a3b8; }
</style>

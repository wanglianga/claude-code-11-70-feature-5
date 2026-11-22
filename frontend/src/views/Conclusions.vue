<template>
  <el-card>
    <template #header>
      <div class="head">
        <span>质量结论（范围 / 时序 / 设备状态三项检查）</span>
        <div class="filters">
          <el-select v-model="conclusion" placeholder="结论" size="small" clearable style="width: 150px" @change="load(1)">
            <el-option label="正常" value="normal" />
            <el-option label="超标" value="exceed" />
            <el-option label="待核（校准/维护/时序异常）" value="pending_review" />
          </el-select>
          <el-button size="small" @click="load(1)">刷新</el-button>
        </div>
      </div>
    </template>
    <el-table :data="items" size="small" border v-loading="loading">
      <el-table-column prop="id" label="#" width="80" />
      <el-table-column prop="receive_log_id" label="日志ID" width="90" />
      <el-table-column prop="device_point" label="点位" width="120" />
      <el-table-column prop="partition_key" label="分区" width="100" />
      <el-table-column prop="policy_version" label="策略版本" width="90" />
      <el-table-column label="范围" width="70">
        <template #default="{ row }"><span :class="row.range_ok ? 'ok' : 'bad'">{{ row.range_ok ? '✓' : '✕' }}</span></template>
      </el-table-column>
      <el-table-column label="时序" width="70">
        <template #default="{ row }"><span :class="row.ts_ok ? 'ok' : 'bad'">{{ row.ts_ok ? '✓' : '✕' }}</span></template>
      </el-table-column>
      <el-table-column label="设备状态" width="80">
        <template #default="{ row }"><span :class="row.device_status_ok ? 'ok' : 'warn'">{{ row.device_status_ok ? '✓' : '✕' }}</span></template>
      </el-table-column>
      <el-table-column label="结论" width="110">
        <template #default="{ row }">
          <el-tag :type="tagType(row.conclusion)" size="small">{{ text(row.conclusion) }}</el-tag>
          <el-tag v-if="!row.confirmed" type="info" size="small" style="margin-left:4px">未确认</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="detail" label="说明" show-overflow-tooltip />
      <el-table-column prop="created_at" label="生成时间(UTC)" width="180" />
    </el-table>
    <el-pagination class="pager" layout="total, prev, pager, next" :total="total" :page-size="20"
      :current-page="page" @current-change="load" />
  </el-card>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import api from '../api'

const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const conclusion = ref('')

function tagType(c) {
  return c === 'normal' ? 'success' : c === 'exceed' ? 'danger' : 'warning'
}
function text(c) {
  return c === 'normal' ? '正常' : c === 'exceed' ? '超标' : '待核'
}

async function load(p = 1) {
  loading.value = true
  try {
    const params = { page: p, page_size: 20 }
    if (conclusion.value) params.conclusion = conclusion.value
    const { data } = await api.get('/quality/conclusions', { params })
    items.value = data.items
    total.value = data.total
    page.value = p
  } finally {
    loading.value = false
  }
}
onMounted(() => load(1))
</script>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; }
.filters { display: flex; gap: 8px; }
.pager { margin-top: 12px; justify-content: flex-end; }
.bad { color: #f56c6c; }
.warn { color: #e6a23c; }
.ok { color: #67c23a; }
</style>

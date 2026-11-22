<template>
  <el-card>
    <template #header>
      <div class="head">
        <span>告警（只消费已确认的高风险超标结论；设备校准/维护的待核结论不进入告警）</span>
        <div>
          <el-radio-group v-model="status" size="small" @change="load(1)">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button value="open">未确认</el-radio-button>
            <el-radio-button value="acked">已确认</el-radio-button>
          </el-radio-group>
        </div>
      </div>
    </template>
    <el-table :data="items" size="small" border v-loading="loading">
      <el-table-column prop="id" label="#" width="70" />
      <el-table-column prop="device_point" label="点位" width="120" />
      <el-table-column prop="message" label="内容" show-overflow-tooltip />
      <el-table-column prop="received_at" label="读数接收时间(UTC)" width="180" />
      <el-table-column prop="created_at" label="告警时间(UTC)" width="180" />
      <el-table-column label="接收→告警" width="130">
        <template #default="{ row }">
          <el-tag :type="row.latency_ms <= 900000 ? 'success' : 'danger'" size="small">
            {{ fmtMs(row.latency_ms) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 'open' ? 'danger' : 'info'" size="small">
            {{ row.status === 'open' ? '未确认' : '已确认' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="160">
        <template #default="{ row }">
          <el-button v-if="row.status === 'open'" size="small" type="primary" @click="ack(row)">确认</el-button>
          <el-button size="small" link type="primary" @click="$router.push(`/evidence?id=${row.receive_log_id}`)">证据链</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination class="pager" layout="total, prev, pager, next" :total="total" :page-size="20"
      :current-page="page" @current-change="load" />
  </el-card>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const items = ref([])
const total = ref(0)
const page = ref(1)
const status = ref('')
const loading = ref(false)

function fmtMs(ms) {
  if (ms < 1000) return `${ms} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`
  return `${(ms / 60000).toFixed(1)} min`
}

async function load(p = 1) {
  loading.value = true
  try {
    const params = { page: p, page_size: 20 }
    if (status.value) params.status = status.value
    const { data } = await api.get('/alerts', { params })
    items.value = data.items
    total.value = data.total
    page.value = p
  } finally {
    loading.value = false
  }
}

async function ack(row) {
  await api.post(`/alerts/${row.id}/ack`)
  ElMessage.success('已确认')
  load(page.value)
}
onMounted(() => load(1))
</script>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; }
.pager { margin-top: 12px; justify-content: flex-end; }
</style>

<template>
  <el-card>
    <template #header>
      <div class="head">
        <span>持久接收日志（id 即全中心接收顺序，监管取证期内不抽样删除）</span>
        <div class="filters">
          <el-input v-model="filters.session_uuid" placeholder="会话 UUID" size="small" clearable style="width: 170px" />
          <el-input v-model="filters.gateway_code" placeholder="网关" size="small" clearable style="width: 120px" />
          <el-input v-model="filters.device_point" placeholder="点位" size="small" clearable style="width: 130px" />
          <el-select v-model="filters.status" placeholder="状态" size="small" clearable style="width: 130px">
            <el-option label="已确认" value="confirmed" />
            <el-option label="重传" value="duplicate" />
            <el-option label="冲突" value="conflict" />
          </el-select>
          <el-button size="small" type="primary" @click="load(1)">查询</el-button>
        </div>
      </div>
    </template>
    <el-table :data="items" size="small" border v-loading="loading">
      <el-table-column prop="id" label="#" width="80" />
      <el-table-column prop="received_at" label="接收时间(UTC)" width="180" />
      <el-table-column prop="gateway_code" label="网关" width="110" />
      <el-table-column prop="session_uuid" label="会话" width="150" show-overflow-tooltip />
      <el-table-column prop="source_seq" label="来源序列" width="90" />
      <el-table-column prop="device_point" label="点位" width="120" />
      <el-table-column prop="value" label="数值" width="90" />
      <el-table-column prop="reading_ts" label="读数时间(UTC)" width="180" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="note" label="备注" show-overflow-tooltip />
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="$router.push(`/evidence?id=${row.id}`)">证据链</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination class="pager" layout="total, prev, pager, next" :total="total" :page-size="20"
      :current-page="page" @current-change="load" />
  </el-card>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import api from '../api'

const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const filters = reactive({ session_uuid: '', gateway_code: '', device_point: '', status: '' })

function statusType(s) {
  return s === 'confirmed' ? 'success' : s === 'duplicate' ? 'info' : 'danger'
}
function statusText(s) {
  return s === 'confirmed' ? '已确认' : s === 'duplicate' ? '重传' : '冲突'
}

async function load(p = 1) {
  loading.value = true
  try {
    const params = { page: p, page_size: 20 }
    for (const k in filters) if (filters[k]) params[k] = filters[k]
    const { data } = await api.get('/logs', { params })
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
.head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.filters { display: flex; gap: 8px; }
.pager { margin-top: 12px; justify-content: flex-end; }
</style>

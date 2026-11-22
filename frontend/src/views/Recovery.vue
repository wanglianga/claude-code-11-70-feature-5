<template>
  <div>
    <el-card class="mb">
      <template #header>
        <div class="head">
          <span>恢复编排（按分区游标回补积压；降级期间自动回补暂停，写入额度留给高风险实时通道）</span>
          <el-button size="small" type="primary" :loading="running" @click="trigger">手动触发全部分区回补</el-button>
        </div>
      </template>
      <el-table :data="partitions" size="small" border>
        <el-table-column prop="partition_key" label="分区" width="110" />
        <el-table-column prop="risk_class" label="风险级" width="90" />
        <el-table-column prop="backlog" label="积压水位" width="100">
          <template #default="{ row }"><span :class="{bad: row.backlog > 0}">{{ row.backlog }}</span></template>
        </el-table-column>
        <el-table-column prop="confirmed_cursor" label="确认游标" width="110" />
        <el-table-column prop="batched_cursor" label="入批游标" width="110" />
        <el-table-column prop="processed_cursor" label="处理游标" width="110" />
        <el-table-column label="缺口（确认-处理）" width="130">
          <template #default="{ row }">{{ row.confirmed_count - row.processed_count }}</template>
        </el-table-column>
        <el-table-column label="操作">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="triggerOne(row.partition_key)">回补该分区</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card>
      <template #header><span>回补运行记录</span></template>
      <el-table :data="runs" size="small" border>
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column prop="partition_key" label="分区" width="110" />
        <el-table-column prop="trigger" label="触发" width="80" />
        <el-table-column prop="from_cursor" label="起始游标" width="100" />
        <el-table-column prop="to_cursor" label="结束游标" width="100" />
        <el-table-column prop="recovered_count" label="回补条数" width="100" />
        <el-table-column prop="status" label="状态" width="80" />
        <el-table-column prop="started_at" label="开始时间(UTC)" width="180" />
        <el-table-column prop="finished_at" label="完成时间(UTC)" width="180" />
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const partitions = ref([])
const runs = ref([])
const running = ref(false)
let timer = null

async function load() {
  const [p, r] = await Promise.all([api.get('/partitions'), api.get('/recovery/runs')])
  partitions.value = p.data
  runs.value = r.data
}
async function trigger() {
  running.value = true
  try {
    const { data } = await api.post('/recovery/run', {})
    ElMessage.success(`回补完成：${data.count} 个分区运行`)
    load()
  } finally {
    running.value = false
  }
}
async function triggerOne(key) {
  await api.post('/recovery/run', { partition_key: key })
  ElMessage.success(`分区 ${key} 回补已触发`)
  load()
}
onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<style scoped>
.mb { margin-bottom: 12px; }
.head { display: flex; justify-content: space-between; align-items: center; }
.bad { color: #f56c6c; font-weight: 600; }
</style>

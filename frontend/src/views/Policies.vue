<template>
  <div>
    <el-card class="mb">
      <template #header>
        <div class="head">
          <span>策略台账（批次在创建时固化策略版本；升级只影响新批次，不回溯改写恢复顺序）</span>
          <el-button size="small" type="primary" @click="dialog = true">发布新版本</el-button>
        </div>
      </template>
      <el-table :data="ledger" size="small" border>
        <el-table-column prop="version" label="版本" width="70" />
        <el-table-column prop="name" label="名称" width="160" />
        <el-table-column label="分区优先级权重" width="220">
          <template #default="{ row }">
            高 {{ row.priority?.high }} / 中 {{ row.priority?.medium }} / 低 {{ row.priority?.low }}
          </template>
        </el-table-column>
        <el-table-column prop="reserve_high_writes" label="高风险写入额度预留(%)" width="170" />
        <el-table-column prop="batch_count" label="引用批次数" width="100" />
        <el-table-column prop="conclusion_count" label="产生结论数" width="100" />
        <el-table-column prop="created_at" label="发布时间(UTC)" width="180" />
        <el-table-column label="状态">
          <template #default="{ row }">
            <el-tag :type="row.active ? 'success' : 'info'" size="small">{{ row.active ? '生效中' : '历史版本' }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card>
      <template #header><span>批次记录</span></template>
      <el-table :data="batches" size="small" border>
        <el-table-column prop="id" label="#" width="90" />
        <el-table-column prop="partition_key" label="分区" width="110" />
        <el-table-column prop="policy_version" label="策略版本" width="90" />
        <el-table-column label="来源" width="100">
          <template #default="{ row }">
            <el-tag :type="row.source === 'recovery' ? 'warning' : 'info'" size="small">
              {{ row.source === 'recovery' ? '恢复回补' : '实时调度' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="recovery_run_id" label="恢复运行ID" width="100">
          <template #default="{ row }">{{ row.recovery_run_id ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="item_count" label="条目数" width="90" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'done' ? 'success' : 'warning'" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间(UTC)" width="180" />
        <el-table-column prop="processed_at" label="完成时间(UTC)" width="180" />
      </el-table>
      <el-pagination class="pager" layout="total, prev, pager, next" :total="batchTotal" :page-size="20"
        :current-page="batchPage" @current-change="loadBatches" />
    </el-card>

    <el-dialog v-model="dialog" title="发布新策略版本" width="440px">
      <el-form label-width="130px">
        <el-form-item label="名称"><el-input v-model="form.name" placeholder="例如：暴雨应急策略" /></el-form-item>
        <el-form-item label="高风险权重"><el-input-number v-model="form.priority.high" :min="0" /></el-form-item>
        <el-form-item label="中风险权重"><el-input-number v-model="form.priority.medium" :min="0" /></el-form-item>
        <el-form-item label="低风险权重"><el-input-number v-model="form.priority.low" :min="0" /></el-form-item>
        <el-form-item label="高风险额度预留%"><el-input-number v-model="form.reserve_high_writes" :min="0" :max="100" /></el-form-item>
        <el-form-item label="立即生效"><el-switch v-model="form.activate" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="create">发布</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const ledger = ref([])
const batches = ref([])
const batchTotal = ref(0)
const batchPage = ref(1)
const dialog = ref(false)
const form = reactive({
  name: '',
  priority: { high: 100, medium: 50, low: 10 },
  reserve_high_writes: 60,
  activate: true,
})

async function load() {
  const { data } = await api.get('/policies/ledger')
  ledger.value = data
}
async function loadBatches(p = 1) {
  const { data } = await api.get('/batches', { params: { page: p, page_size: 20 } })
  batches.value = data.items
  batchTotal.value = data.total
  batchPage.value = p
}
async function create() {
  await api.post('/policies', { ...form, note: '控制台发布' })
  ElMessage.success('已发布新版本')
  dialog.value = false
  load()
}
onMounted(() => {
  load()
  loadBatches(1)
})
</script>

<style scoped>
.mb { margin-bottom: 12px; }
.head { display: flex; justify-content: space-between; align-items: center; }
.pager { margin-top: 12px; justify-content: flex-end; }
</style>

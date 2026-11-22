<template>
  <el-card>
    <template #header>
      <div class="head">
        <span>设备目录（点位 / 因子 / 采样周期 / 风险级别 / 阈值 / 运行状态）</span>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </template>
    <el-table :data="items" size="small" border v-loading="loading">
      <el-table-column prop="point_code" label="点位编码" width="120" />
      <el-table-column prop="name" label="名称" width="160" />
      <el-table-column prop="category" label="类别" width="110">
        <template #default="{ row }">{{ catText(row.category) }}</template>
      </el-table-column>
      <el-table-column prop="factor" label="因子" width="90" />
      <el-table-column prop="unit" label="单位" width="70" />
      <el-table-column prop="sampling_period_sec" label="采样周期(s)" width="100" />
      <el-table-column label="风险级" width="90">
        <template #default="{ row }">
          <el-tag :type="row.risk_level === 'high' ? 'danger' : row.risk_level === 'medium' ? 'warning' : 'info'" size="small">
            {{ row.risk_level }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="阈值" width="120">
        <template #default="{ row }">[{{ row.threshold_min ?? '-∞' }}, {{ row.threshold_max ?? '+∞' }}]</template>
      </el-table-column>
      <el-table-column prop="partition_key" label="分区" width="100" />
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag :type="row.status === 'normal' ? 'success' : 'warning'" size="small">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="openEdit(row)">编辑</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dialog" :title="`编辑 ${form.name}`" width="420px">
      <el-form label-width="90px">
        <el-form-item label="运行状态">
          <el-select v-model="form.status" style="width: 100%">
            <el-option label="正常" value="normal" />
            <el-option label="校准中" value="calibrating" />
            <el-option label="维护中" value="maintenance" />
          </el-select>
        </el-form-item>
        <el-form-item label="阈值下限">
          <el-input-number v-model="form.threshold_min" :controls="false" style="width: 100%" />
        </el-form-item>
        <el-form-item label="阈值上限">
          <el-input-number v-model="form.threshold_max" :controls="false" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const items = ref([])
const loading = ref(false)
const dialog = ref(false)
const form = reactive({ id: null, name: '', status: 'normal', threshold_min: null, threshold_max: null })

function catText(c) {
  return { wastewater: '废水', waste_gas: '废气', tank_level: '储罐液位', rain_valve: '雨污阀门' }[c] || c
}
function statusText(s) {
  return { normal: '正常', calibrating: '校准中', maintenance: '维护中' }[s] || s
}

async function load() {
  loading.value = true
  try {
    const { data } = await api.get('/devices')
    items.value = data
  } finally {
    loading.value = false
  }
}

function openEdit(row) {
  Object.assign(form, {
    id: row.id,
    name: row.name,
    status: row.status,
    threshold_min: row.threshold_min,
    threshold_max: row.threshold_max,
  })
  dialog.value = true
}

async function save() {
  await api.patch(`/devices/${form.id}`, {
    status: form.status,
    threshold_min: form.threshold_min,
    threshold_max: form.threshold_max,
  })
  ElMessage.success('已保存')
  dialog.value = false
  load()
}
onMounted(load)
</script>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; }
</style>

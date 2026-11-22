<template>
  <div>
    <el-row :gutter="12">
      <el-col :span="12">
        <el-card class="mb">
          <template #header><span>场景一：实时流</span></template>
          <p class="desc">各设备以当前时间上报，部分高风险因子超标。验证：接收日志确认 → 质量结论 → 15 分钟内告警。</p>
          <el-input-number v-model="rt.count" :min="1" :max="500" size="small" /> 条
          <el-button type="primary" size="small" :loading="loading" @click="run('realtime')">注入实时流</el-button>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card class="mb">
          <template #header><span>场景二：断网恢复补传流</span></template>
          <p class="desc">网关重连后把数小时缓存读数带新会话集中上传。验证：恢复编排器按分区游标回补，分区最终一致。</p>
          <el-input-number v-model="bf.hours" :min="0.1" :max="6" :step="0.5" size="small" /> 小时
          <el-button type="primary" size="small" :loading="loading" @click="run('backfill')">注入补传流</el-button>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card class="mb">
          <template #header><span>场景三：冲突重传</span></template>
          <p class="desc">同会话同序列：先发正常值，再同指纹重传，最后发不同载荷。验证：重传只确认一次；冲突并列存证不覆盖。</p>
          <el-button type="primary" size="small" :loading="loading" @click="run('conflict')">注入冲突重传</el-button>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card class="mb">
          <template #header><span>场景四：数据库写入持续变慢</span></template>
          <p class="desc">开启后每次写入注入延迟，跨过高风险阈值触发降级：暂停低风险衍生计算，高风险分区保留写入额度。</p>
          <el-switch v-model="slow.enabled" active-text="开启慢写入" @change="toggleSlow" />
          <el-input-number v-model="slow.delay_ms" :min="100" :max="5000" :step="100" size="small" style="margin-left: 12px" /> ms
        </el-card>
      </el-col>
    </el-row>

    <el-card>
      <template #header><span>执行结果</span></template>
      <pre class="result">{{ result || '（尚未执行）' }}</pre>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const rt = reactive({ count: 30 })
const bf = reactive({ hours: 2 })
const slow = reactive({ enabled: false, delay_ms: 1200 })
const loading = ref(false)
const result = ref('')

async function run(kind) {
  loading.value = true
  try {
    const body = kind === 'realtime' ? rt : kind === 'backfill' ? bf : {}
    const { data } = await api.post(`/simulate/${kind}`, body)
    result.value = JSON.stringify(data, null, 2)
    ElMessage.success('场景已注入，请到运行台/接收日志/恢复编排查看')
  } finally {
    loading.value = false
  }
}

async function toggleSlow(val) {
  const { data } = await api.post('/simulate/slow-writes', { enabled: val, delay_ms: slow.delay_ms })
  result.value = JSON.stringify(data, null, 2)
  ElMessage.warning(val ? '慢写入已开启，观察运行台降级状态' : '慢写入已关闭，观察调度器恢复')
}

onMounted(async () => {
  const { data } = await api.get('/simulate/flags')
  slow.enabled = data.slow_write_enabled
  slow.delay_ms = data.slow_write_delay_ms || 1200
})
</script>

<style scoped>
.mb { margin-bottom: 12px; }
.desc { color: #606266; font-size: 13px; min-height: 40px; }
.result { background: #0f2a43; color: #b8c7d6; padding: 12px; border-radius: 4px; max-height: 320px; overflow: auto; }
</style>

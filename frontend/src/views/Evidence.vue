<template>
  <el-card>
    <template #header>
      <div class="head">
        <span>证据链（原始读数 → 接收日志 → 网关会话 → 批次 → 质量结论 → 告警，含并列冲突证据）</span>
        <div class="search">
          <el-input-number v-model="logId" :min="1" placeholder="接收日志 ID" size="small" />
          <el-button size="small" type="primary" @click="load">查询</el-button>
        </div>
      </div>
    </template>

    <el-empty v-if="!data" description="输入接收日志 ID 查询完整证据链" />
    <template v-else>
      <el-alert v-if="data.is_conflict_entry" type="warning" :closable="false" class="mb"
        :title="`该日志为冲突证据：序列与已确认日志 #${data.conflict_kept_log_id} 相同但载荷不同，并列保存，未覆盖已确认读数`" />
      <el-steps :active="stepsActive" align-center finish-status="success" class="mb">
        <el-step title="网关接收" :description="`#${log.id} · ${log.received_at}`" />
        <el-step title="日志确认" :description="statusText(log.status)" />
        <el-step title="入批处理" :description="data.batch ? `批次 #${data.batch.id} · 策略v${data.batch.policy_version}` : '待入批'" />
        <el-step title="质量结论" :description="data.conclusion ? conclusionText(data.conclusion) : '待生成'" />
        <el-step title="告警" :description="data.alert ? `告警 #${data.alert.id} · 延迟 ${data.alert.latency_ms}ms` : '无告警'" />
      </el-steps>

      <el-descriptions :column="3" border size="small" class="mb">
        <el-descriptions-item label="日志 ID">#{{ log.id }}</el-descriptions-item>
        <el-descriptions-item label="网关">{{ log.gateway_code }}</el-descriptions-item>
        <el-descriptions-item label="会话">{{ log.session_uuid }}</el-descriptions-item>
        <el-descriptions-item label="来源序列">{{ log.source_seq }}</el-descriptions-item>
        <el-descriptions-item label="载荷指纹">{{ log.fingerprint }}</el-descriptions-item>
        <el-descriptions-item label="点位">{{ log.device_point }}</el-descriptions-item>
        <el-descriptions-item label="读数时间(UTC)">{{ log.reading_ts }}</el-descriptions-item>
        <el-descriptions-item label="数值">{{ log.value }}</el-descriptions-item>
        <el-descriptions-item label="接收时设备状态">{{ log.device_status_at_receipt || '-' }}</el-descriptions-item>
      </el-descriptions>

      <el-row :gutter="12">
        <el-col :span="8">
          <el-card shadow="never">
            <template #header><span>设备</span></template>
            <template v-if="data.device">
              <p>{{ data.device.name }}（{{ data.device.point_code }}）</p>
              <p>因子：{{ data.device.factor }} · 风险级：{{ data.device.risk_level }}</p>
              <p>分区：{{ data.device.partition_key }} · 阈值：[{{ data.device.threshold_min ?? '-∞' }}, {{ data.device.threshold_max ?? '+∞' }}]</p>
            </template>
            <el-empty v-else description="无" :image-size="40" />
          </el-card>
        </el-col>
        <el-col :span="8">
          <el-card shadow="never">
            <template #header><span>质量结论</span></template>
            <template v-if="data.conclusion">
              <p>结论：<el-tag size="small" :type="data.conclusion.conclusion === 'exceed' ? 'danger' : data.conclusion.conclusion === 'normal' ? 'success' : 'warning'">{{ conclusionText(data.conclusion) }}</el-tag></p>
              <p>范围 {{ yn(data.conclusion.range_ok) }} · 时序 {{ yn(data.conclusion.ts_ok) }} · 设备 {{ yn(data.conclusion.device_status_ok) }}</p>
              <p>{{ data.conclusion.detail }}</p>
            </template>
            <el-empty v-else description="待生成" :image-size="40" />
          </el-card>
        </el-col>
        <el-col :span="8">
          <el-card shadow="never">
            <template #header><span>并列冲突证据（{{ data.conflicts.length }}）</span></template>
            <template v-if="data.conflicts.length">
              <p v-for="c in data.conflicts" :key="c.log_id">
                日志 #{{ c.log_id }} · 值 {{ c.value }} · {{ c.received_at }}
              </p>
              <p class="hint">后到值与已确认读数并列保存，未覆盖。</p>
            </template>
            <el-empty v-else description="无冲突" :image-size="40" />
          </el-card>
        </el-col>
      </el-row>
    </template>
  </el-card>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'

const route = useRoute()
const logId = ref(route.query.id ? Number(route.query.id) : 1)
const data = ref(null)
const log = computed(() => data.value?.receive_log || {})

const stepsActive = computed(() => {
  if (!data.value) return 0
  if (data.value.alert) return 5
  if (data.value.conclusion) return 4
  if (data.value.batch) return 3
  return 2
})

function statusText(s) {
  return { confirmed: '已确认', duplicate: '重传（未重复确认）', conflict: '冲突（并列存证）' }[s] || s
}
function conclusionText(c) {
  const t = { normal: '正常', exceed: '超标', pending_review: '待核' }[c.conclusion] || c.conclusion
  return c.confirmed ? t : `${t}（未确认）`
}
function yn(v) {
  return v ? '✓' : '✕'
}

async function load() {
  const { data: d } = await api.get(`/evidence/${logId.value}`)
  data.value = d
}
onMounted(() => {
  if (route.query.id) load()
})
</script>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: center; }
.search { display: flex; gap: 8px; }
.mb { margin-bottom: 12px; }
.hint { color: #909399; font-size: 12px; }
</style>

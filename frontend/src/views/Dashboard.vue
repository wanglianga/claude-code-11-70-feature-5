<template>
  <div>
    <el-alert v-if="summary.degraded" type="error" :closable="false" show-icon class="mb"
      title="写入延迟跨过高风险阈值：调度器已降级 —— 暂停低风险衍生计算，为高风险分区保留写入额度" />
    <el-row :gutter="12">
      <el-col :span="4"><el-card><div class="num">{{ summary.total_received ?? '-' }}</div><div class="lbl">接收日志总数</div></el-card></el-col>
      <el-col :span="4"><el-card><div class="num ok">{{ summary.confirmed ?? '-' }}</div><div class="lbl">已确认读数</div></el-card></el-col>
      <el-col :span="4"><el-card><div class="num">{{ summary.duplicate ?? '-' }}</div><div class="lbl">重传（只确认一次）</div></el-card></el-col>
      <el-col :span="4"><el-card><div class="num" :class="{warn: summary.conflict}">{{ summary.conflict ?? '-' }}</div><div class="lbl">冲突证据</div></el-card></el-col>
      <el-col :span="4"><el-card><div class="num" :class="{warn: summary.pending_review}">{{ summary.pending_review ?? '-' }}</div><div class="lbl">待核结论</div></el-card></el-col>
      <el-col :span="4"><el-card><div class="num" :class="{bad: summary.open_alerts}">{{ summary.open_alerts ?? '-' }}</div><div class="lbl">未确认告警</div></el-card></el-col>
    </el-row>

    <el-row :gutter="12" class="mb">
      <el-col :span="12">
        <el-card>
          <template #header>
            <div class="card-head">
              <span>数据库写入延迟（阈值 {{ summary.write_latency_threshold_ms }} ms）</span>
              <el-tag :type="summary.degraded ? 'danger' : 'success'" size="small">
                {{ summary.degraded ? '降级中' : '正常' }} · 最近 {{ fmtMs(summary.last_write_latency_ms) }}
              </el-tag>
            </div>
          </template>
          <svg v-if="metrics.length" viewBox="0 0 600 120" class="spark">
            <line :x1="0" :y1="thresholdY" :x2="600" :y2="thresholdY" stroke="#f56c6c" stroke-dasharray="4" />
            <polyline :points="sparkPoints" fill="none" stroke="#409eff" stroke-width="1.5" />
          </svg>
          <el-empty v-else description="暂无探针数据" :image-size="60" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header><span>告警延迟（目标 ≤ 15 分钟）</span></template>
          <div class="latency">
            <div><div class="num" :class="latencyClass(summary.max_alert_latency_ms)">{{ fmtMs(summary.max_alert_latency_ms) }}</div><div class="lbl">最大 接收→告警</div></div>
            <div><div class="num">{{ fmtMs(summary.avg_alert_latency_ms) }}</div><div class="lbl">平均 接收→告警</div></div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card>
      <template #header><span>分区积压水位与游标</span></template>
      <el-table :data="partitions" size="small" border>
        <el-table-column prop="partition_key" label="分区" width="110" />
        <el-table-column label="风险级" width="90">
          <template #default="{ row }">
            <el-tag :type="riskType(row.risk_class)" size="small">{{ row.risk_class }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="backlog" label="积压水位" width="90">
          <template #default="{ row }"><span :class="{bad: row.backlog > 0}">{{ row.backlog }}</span></template>
        </el-table-column>
        <el-table-column prop="inflight" label="在途批次" width="90" />
        <el-table-column prop="confirmed_count" label="已确认" width="90" />
        <el-table-column prop="batched_count" label="已入批" width="90" />
        <el-table-column prop="processed_count" label="已处理" width="90" />
        <el-table-column prop="confirmed_cursor" label="确认游标" width="100" />
        <el-table-column prop="processed_cursor" label="处理游标" width="100" />
        <el-table-column label="完整性">
          <template #default="{ row }">
            <el-tag :type="row.consistent ? 'success' : 'warning'" size="small">
              {{ row.consistent ? '一致' : '追补中' }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import api from '../api'

const summary = ref({})
const partitions = ref([])
const metrics = ref([])
const sparkPoints = ref('')
const thresholdY = ref(60)
let timer = null

function fmtMs(ms) {
  if (ms === null || ms === undefined) return '-'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`
  return `${(ms / 60000).toFixed(1)} min`
}
function latencyClass(ms) {
  return ms && ms > 15 * 60 * 1000 ? 'bad' : 'ok'
}
function riskType(cls) {
  return cls === 'high' ? 'danger' : cls === 'medium' ? 'warning' : 'info'
}

async function load() {
  const [s, p, m] = await Promise.all([
    api.get('/dashboard/summary'),
    api.get('/partitions'),
    api.get('/dashboard/write-metrics', { params: { limit: 60 } }),
  ])
  summary.value = s.data
  partitions.value = p.data
  metrics.value = m.data.filter((x) => x.latency_ms >= 0)
  const vals = metrics.value.map((x) => x.latency_ms)
  const maxV = Math.max(...vals, summary.value.write_latency_threshold_ms || 800, 1)
  const y = (v) => 110 - (v / maxV) * 100
  thresholdY.value = y(summary.value.write_latency_threshold_ms || 800)
  sparkPoints.value = vals.map((v, i) => `${(i / Math.max(vals.length - 1, 1)) * 600},${y(v)}`).join(' ')
}

onMounted(() => {
  load()
  timer = setInterval(load, 3000)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<style scoped>
.mb { margin-bottom: 12px; }
.num { font-size: 26px; font-weight: 600; color: #303133; }
.num.ok { color: #67c23a; }
.num.warn { color: #e6a23c; }
.num.bad, .bad { color: #f56c6c; font-weight: 600; }
.lbl { color: #909399; font-size: 12px; margin-top: 4px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.spark { width: 100%; height: 120px; }
.latency { display: flex; gap: 60px; }
</style>

<template>
  <div>
    <!-- 策略与成本边界 -->
    <el-card class="mb">
      <template #header>
        <div class="head">
          <span>台账保留模式与重建成本边界（按复盘响应时间 / 峰值事件量 / 监管保留期取舍）</span>
          <el-tag :type="cfg.ledger_mode?.mode === 'journal' ? 'warning' : 'success'" size="small">
            当前：{{ cfg.ledger_mode?.label }}
          </el-tag>
        </div>
      </template>
      <el-alert type="info" :closable="false" show-icon class="mb"
        :title="`建议模式：${cfg.recommendation?.label || '-'}`"
        :description="(cfg.recommendation?.reasons || []).join('；')" />
      <el-row :gutter="12">
        <el-col :span="14">
          <el-table :data="costRows" size="small" border>
            <el-table-column prop="metric" label="指标" width="170" />
            <el-table-column prop="journal" label="逐次比较日志" />
            <el-table-column prop="checkpoint" label="检查点 + 事件序列" />
          </el-table>
        </el-col>
        <el-col :span="10">
          <el-form label-width="112px" size="small">
            <el-form-item label="响应目标(秒)">
              <el-input-number v-model="q.response_target_sec" :min="0.1" :step="0.5" />
            </el-form-item>
            <el-form-item label="峰值事件/秒">
              <el-input-number v-model="q.peak_event_rate" :min="1" :step="10" />
            </el-form-item>
            <el-form-item label="保留期(天)">
              <el-input-number v-model="q.retention_days" :min="1" :step="30" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" size="small" @click="askStrategy">按参数重新建议</el-button>
            </el-form-item>
          </el-form>
          <el-alert v-if="strategy" type="success" :closable="false"
            :title="`建议 ${strategy.label}`"
            :description="(strategy.reasons || []).join('；')" />
        </el-col>
      </el-row>
    </el-card>

    <!-- 场景序列 -->
    <el-card class="mb">
      <template #header>
        <div class="head">
          <span>模拟序列（容量耗尽 / 低风险阻塞 / 策略切换竞争 / 证据不足保护）</span>
          <span class="hint">模拟通道与生产台账隔离，不参与真实调度</span>
        </div>
      </template>
      <el-row :gutter="12">
        <el-col :span="6" v-for="s in lib" :key="s.key">
          <el-card shadow="hover" class="sc-card">
            <div class="sc-title">{{ s.label }}</div>
            <div class="sc-desc">{{ s.desc }}</div>
            <div class="sc-actions">
              <el-tag v-if="built[s.key]" size="small" :type="verdictType(built[s.key].latest_review?.diagnosis?.verdict)">
                {{ verdictText(built[s.key].latest_review?.diagnosis?.verdict) }}
              </el-tag>
              <el-button size="small" type="primary" :loading="busy === s.key" @click="build(s.key, true)">
                {{ built[s.key] ? '重建' : '构建' }}
              </el-button>
              <el-button size="small" :disabled="!built[s.key]" @click="select(s.key)">查看</el-button>
            </div>
          </el-card>
        </el-col>
      </el-row>
    </el-card>

    <!-- 复盘详情 -->
    <template v-if="cur">
      <el-card class="mb">
        <template #header>
          <div class="head">
            <span>
              复盘 · {{ cur.label }}
              <el-tag size="small" class="ml">v{{ cur.review_version }}</el-tag>
              <el-tag size="small" class="ml" type="info">{{ cur.lane }}</el-tag>
            </span>
            <div class="actions">
              <el-button size="small" @click="lateEvent">补传晚到 → 新版本</el-button>
              <el-button size="small" type="primary" @click="rebuild">按事件序列重放</el-button>
            </div>
          </div>
        </template>
        <el-alert class="mb" :type="verdictType(diag.verdict)" :closable="false" show-icon
          :title="`结论：${verdictText(diag.verdict)}`" :description="diag.summary" />
        <el-descriptions :column="3" border size="small" class="mb">
          <el-descriptions-item label="复盘精度">{{ latest.precision }}</el-descriptions-item>
          <el-descriptions-item label="台账模式">{{ latest.ledger_mode?.label }}</el-descriptions-item>
          <el-descriptions-item label="成本边界">{{ latest.cost_note || '-' }}</el-descriptions-item>
          <el-descriptions-item label="区间开始(UTC)">{{ latest.window?.start }}</el-descriptions-item>
          <el-descriptions-item label="区间结束(UTC)">{{ latest.window?.end }}</el-descriptions-item>
          <el-descriptions-item label="内容哈希">{{ latest.content_hash }}</el-descriptions-item>
        </el-descriptions>
        <el-alert v-if="ordering.differs" type="warning" :closable="false" show-icon class="mb"
          title="同刻事件：数据库写入顺序与预登记序号裁定顺序不一致，已按预登记 event_seq 裁定（不依赖写入先后）" />
        <el-table v-if="ordering.by_db_write_order" :data="orderingRows" size="small" border class="mb">
          <el-table-column prop="event_seq" label="预登记序号" width="120" />
          <el-table-column prop="kind" label="事件类型" width="150" />
          <el-table-column prop="db" label="数据库写入先后" width="160" />
          <el-table-column prop="order" label="顺序规范器裁定" />
        </el-table>
      </el-card>

      <!-- 决策时间线 -->
      <el-card class="mb">
        <template #header><span>决策时间线（每一步的可选批次评分与实际选择）</span></template>
        <el-table :data="steps" size="small" border>
          <el-table-column prop="event_seq" label="事件序号" width="90" />
          <el-table-column prop="at" label="生效时刻(UTC)" width="180" />
          <el-table-column label="分区" width="110">
            <template #default="{ row }">{{ row.partition_key }}</template>
          </el-table-column>
          <el-table-column label="策略/降级" width="150">
            <template #default="{ row }">
              v{{ row.policy_version }}
              <el-tag v-if="row.scoring?.degraded" type="warning" size="small" class="ml">降级</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="可选批次（权重/积压）" min-width="260">
            <template #default="{ row }">
              <span v-for="c in row.candidates" :key="c.partition_key" class="cand"
                :class="{ picked: c.partition_key === row.selected }">
                {{ c.partition_key }}({{ c.weight }}/{{ c.backlog }})
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="selected" label="实际选择" width="110" />
          <el-table-column prop="explain" label="说明" min-width="240" />
        </el-table>
        <el-timeline class="tl">
          <el-timeline-item v-for="e in visibleEvents" :key="e.event_seq" :timestamp="e.effective_ts"
            placement="top" :type="kindType(e.kind)">
            <div>
              <el-tag size="small" :type="kindType(e.kind)">{{ kindText(e.kind) }}</el-tag>
              <span class="ml">#{{ e.event_seq }}</span>
              <span class="ml muted" v-if="e.partition_key">{{ e.partition_key }}</span>
              <span class="ml">{{ e.note }}</span>
            </div>
          </el-timeline-item>
        </el-timeline>
        <div v-if="(timeline.events || []).length > 200" class="hint">
          时间线共 {{ timeline.events.length }} 个事件，仅展示最近 200 个（完整序列见下方证据与版本历史）
        </div>
        <el-alert v-if="!timeline.chain_complete" type="error" :closable="false" show-icon
          :title="'事件链断裂：' + (timeline.chain_gaps || []).join('；')" />
      </el-card>

      <!-- 反事实比较 -->
      <el-card class="mb">
        <template #header><span>反事实比较（不修改生产台账）</span></template>
        <template v-if="cf.status === 'ok'">
          <el-row :gutter="12">
            <el-col :span="12" v-for="c in cf.comparisons" :key="c.name">
              <el-card shadow="never" class="cf-card">
                <div class="cf-name">{{ c.name }}</div>
                <template v-if="c.status === 'insufficient'">
                  <el-alert type="warning" :closable="false" :title="c.explain" />
                </template>
                <template v-else>
                  <div class="cf-nums">
                    <div><span class="k">实际等待</span><span class="v">{{ fmtMs(c.actual_wait_ms) }}</span></div>
                    <div><span class="k">反事实等待</span><span class="v">{{ fmtMs(c.counterfactual_wait_ms) }}</span></div>
                    <div>
                      <span class="k">等待差异</span>
                      <span class="v" :class="deltaClass(c.wait_delta_ms)">{{ signed(c.wait_delta_ms) }}</span>
                    </div>
                  </div>
                  <div class="cf-explain">{{ c.explain }}</div>
                </template>
              </el-card>
            </el-col>
          </el-row>
          <div class="hint mt">{{ cf.evidence_note }}</div>
        </template>
        <template v-else>
          <el-alert type="warning" :closable="false" show-icon title="证据不足：比较器不臆测历史答案"
            :description="(cf.reasons || []).join('；')" class="mb" />
          <el-alert type="info" :closable="false" title="下列为当前队列状态，仅作说明，并非当时历史答案">
            <div v-for="(v, k) in (cf.current_queue_state_not_historical || {})" :key="k" class="hint">
              {{ k }}：积压 {{ v.backlog }} · 在途 {{ v.inflight }}
            </div>
          </el-alert>
        </template>
      </el-card>

      <!-- 证据下钻 -->
      <el-card class="mb">
        <template #header><span>证据下钻（结论 → 读数 / 策略 / 处理事实）</span></template>
        <el-table :data="latest.evidence_index || []" size="small" border>
          <el-table-column prop="kind" label="类型" width="110" />
          <el-table-column prop="ref" label="引用" width="180" />
          <el-table-column prop="label" label="内容" />
          <el-table-column prop="explain" label="说明" />
          <el-table-column label="操作" width="120">
            <template #default="{ row }">
              <el-button v-if="row.kind === 'reading'" size="small" link type="primary"
                @click="drill(row.ref)">查看证据链</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 版本历史与差异 -->
      <el-card>
        <template #header><span>复盘版本历史（已签发结论只追加，不改写）</span></template>
        <el-table :data="cur.history || []" size="small" border>
          <el-table-column prop="version" label="版本" width="80" />
          <el-table-column label="结论" width="150">
            <template #default="{ row }">
              <el-tag size="small" :type="verdictType(row.verdict)">{{ verdictText(row.verdict) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="precision" label="精度" width="140" />
          <el-table-column prop="supersedes_version" label="替代版本" width="100" />
          <el-table-column prop="issued_at" label="签发时间(UTC)" width="180" />
          <el-table-column prop="content_hash" label="内容哈希" width="180" />
          <el-table-column label="差异说明">
            <template #default="{ row }">
              <span v-if="row.diff">{{ (row.diff.changes || []).join('；') }}</span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>

    <!-- 生产台账：告警 / 区间复盘 -->
    <el-card class="mt">
      <template #header>
        <div class="head">
          <span>生产台账复盘（从指定告警或延迟区间重建，反事实只读计算）</span>
          <el-button size="small" @click="loadLive">刷新告警</el-button>
        </div>
      </template>
      <el-table :data="liveAlerts" size="small" border max-height="280">
        <el-table-column prop="alert_id" label="告警#" width="80" />
        <el-table-column prop="receive_log_id" label="读数#" width="80" />
        <el-table-column prop="device_point" label="点位" width="120" />
        <el-table-column prop="partition_key" label="分区" width="100" />
        <el-table-column prop="value" label="数值" width="80" />
        <el-table-column label="接收→入批等待" width="140">
          <template #default="{ row }">{{ fmtMs(row.wait_ms) }}</template>
        </el-table-column>
        <el-table-column label="告警延迟" width="120">
          <template #default="{ row }">{{ fmtMs(row.alert_latency_ms) }}</template>
        </el-table-column>
        <el-table-column label="策略版本" width="90">
          <template #default="{ row }">v{{ row.policy_version ?? '-' }}</template>
        </el-table-column>
        <el-table-column prop="received_at" label="接收时间(UTC)" />
        <el-table-column label="操作" width="110">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="reviewAlert(row)">复盘</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="hint mt">区间复盘：填入延迟区间后重建该区间首个告警的决策时间线。</div>
      <div class="actions mt">
        <el-date-picker v-model="interval.start" type="datetime" placeholder="区间开始" size="small" />
        <el-date-picker v-model="interval.end" type="datetime" placeholder="区间结束" size="small" />
        <el-button size="small" type="primary" @click="reviewInterval">复盘该区间</el-button>
      </div>
      <div v-if="liveResult" class="live-result">
        <el-alert :type="verdictType(liveResult.verdict)" :closable="false" show-icon class="mb"
          :title="`复盘 v${liveResult.version}：${verdictText(liveResult.verdict)}`"
          :description="liveResult.summary" />
        <el-table :data="liveResult.result?.counterfactual?.comparisons || []" size="small" border>
          <el-table-column prop="name" label="反事实" width="200" />
          <el-table-column label="等待差异">
            <template #default="{ row }">{{ signed(row.wait_delta_ms) }}</template>
          </el-table-column>
          <el-table-column prop="explain" label="说明" />
        </el-table>
      </div>
    </el-card>

    <el-drawer v-model="drawer" title="读数证据链" size="45%">
      <div v-if="evidence">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="日志#">{{ evidence.receive_log?.id }}</el-descriptions-item>
          <el-descriptions-item label="点位">{{ evidence.receive_log?.device_point }}</el-descriptions-item>
          <el-descriptions-item label="数值">{{ evidence.receive_log?.value }}</el-descriptions-item>
          <el-descriptions-item label="接收时间">{{ evidence.receive_log?.received_at }}</el-descriptions-item>
          <el-descriptions-item label="批次">#{{ evidence.batch?.id }} · 策略 v{{ evidence.batch?.policy_version }}</el-descriptions-item>
          <el-descriptions-item label="结论">{{ evidence.conclusion?.conclusion }}（{{ evidence.conclusion?.detail }}）</el-descriptions-item>
          <el-descriptions-item label="告警">{{ evidence.alert ? `#${evidence.alert.id} 延迟 ${evidence.alert.latency_ms}ms` : '无' }}</el-descriptions-item>
        </el-descriptions>
      </div>
      <el-empty v-else description="加载中" />
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'

const cfg = ref({})
const built = ref({})
const curKey = ref('')
const busy = ref('')
const strategy = ref(null)
const q = reactive({ response_target_sec: 5, peak_event_rate: 50, retention_days: 365 })
const liveAlerts = ref([])
const liveResult = ref(null)
const interval = reactive({ start: null, end: null })
const drawer = ref(false)
const evidence = ref(null)

const lib = [
  { key: 'capacity', label: '容量耗尽', desc: '单通道被高风险积压写满，目标批次容量释放后才生成。' },
  { key: 'low_risk', label: '低风险阻塞', desc: '降级期间低风险批次占用通道，高风险目标被推迟。' },
  { key: 'policy_race', label: '策略切换竞争', desc: '切换与批次完成同刻，按预登记序号裁定。' },
  { key: 'insufficient', label: '证据不足保护', desc: '事件链断裂/缺水位时只给证据不足，不臆测。' },
]

const cur = computed(() => built.value[curKey.value])
const latest = computed(() => cur.value?.latest_review || {})
const diag = computed(() => latest.value.diagnosis || {})
const timeline = computed(() => latest.value.timeline || {})
const cf = computed(() => latest.value.counterfactual || {})
const steps = computed(() => (timeline.value.steps || []).slice())
const visibleEvents = computed(() => (timeline.value.events || []).slice(-200))
const ordering = computed(() => cur.value?.detail?.ordering_check || {})
const orderingRows = computed(() => {
  const o = ordering.value
  if (!o.by_db_write_order) return []
  const dbOrder = {}
  o.by_db_write_order.forEach((x, i) => (dbOrder[x[0]] = i + 1))
  const seqOrder = {}
  o.by_registered_seq.forEach((x, i) => (seqOrder[x[0]] = i + 1))
  return o.by_registered_seq.map((x) => ({
    event_seq: x[1],
    kind: kindText(x[2]),
    db: `第 ${dbOrder[x[0]]} 位`,
    order: `第 ${seqOrder[x[0]]} 位`,
  }))
})
const costRows = computed(() => {
  const c = cfg.value.cost || {}
  if (!c.journal) return []
  return [
    { metric: '每日写入量', journal: c.journal.writes_per_day?.toLocaleString(), checkpoint: c.checkpoint.writes_per_day?.toLocaleString() },
    { metric: '存储/天 (MB)', journal: c.journal.storage_mb_per_day, checkpoint: c.checkpoint.storage_mb_per_day },
    { metric: '保留期存储 (GB/年)', journal: c.journal.storage_gb_per_year, checkpoint: c.checkpoint.storage_gb_per_year },
    { metric: '查询解释方式', journal: c.journal.query_explanation, checkpoint: c.checkpoint.query_explanation },
    { metric: '重放成本', journal: '无需重放', checkpoint: `${c.checkpoint.replay_cost_sec_per_hour_window}s / 小时区间` },
  ]
})

function fmtMs(ms) {
  if (ms === null || ms === undefined) return '-'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`
  return `${(ms / 60000).toFixed(1)} min`
}
function signed(ms) {
  if (ms === null || ms === undefined) return '-'
  return `${ms > 0 ? '+' : ''}${fmtMs(ms)}`
}
function deltaClass(ms) {
  if (!ms) return ''
  return ms < 0 ? 'ok' : 'warn'
}
function verdictText(v) {
  return {
    capacity_exhausted: '容量耗尽', low_risk_blocked: '低风险阻塞', policy_switch_race: '策略切换竞争',
    no_significant_wait: '无显著等待', insufficient: '证据不足', unknown: '无法判定',
  }[v] || v || '-'
}
function verdictType(v) {
  return {
    capacity_exhausted: 'danger', low_risk_blocked: 'warning', policy_switch_race: 'primary',
    no_significant_wait: 'success', insufficient: 'info',
  }[v] || 'info'
}
function kindText(k) {
  return { receive: '接收', batch_create: '批次生成', policy_effective: '策略生效', batch_complete: '批次完成', decision: '决策', checkpoint: '检查点', late_arrival: '补传晚到' }[k] || k
}
function kindType(k) {
  return { receive: 'primary', batch_create: 'success', policy_effective: 'warning', batch_complete: 'success', decision: 'info', late_arrival: 'danger' }[k] || 'info'
}

async function loadConfig() {
  const { data } = await api.get('/review/config')
  cfg.value = data
}
async function loadList() {
  const { data } = await api.get('/review/scenarios')
  for (const s of data) {
    await select(s.scenario_key, false)
  }
}
async function build(key, force) {
  busy.value = key
  try {
    const { data } = await api.post(`/review/scenarios/${key}/build`, null, { params: { force } })
    built.value[key] = data
    curKey.value = key
    ElMessage.success(`场景「${key}」已构建并签发复盘`)
  } finally {
    busy.value = ''
  }
}
async function select(key, msg = true) {
  const { data } = await api.get(`/review/scenarios/${key}`).catch(() => ({ data: null }))
  if (data) built.value[key] = data
  curKey.value = key
  if (msg && !data) ElMessage.warning('该场景尚未构建')
}
async function rebuild() {
  busy.value = curKey.value
  try {
    const { data } = await api.post(`/review/scenarios/${curKey.value}/rebuild`)
    built.value[curKey.value] = data
    ElMessage.success('已按事件序列重放并签发')
  } finally {
    busy.value = ''
  }
}
async function lateEvent() {
  const { data } = await api.post(`/review/scenarios/${curKey.value}/late-event`)
  built.value[curKey.value] = data
  ElMessage.warning('补传晚到已落入已复盘区间：已签发旧版本保持不变，生成新版本与差异说明')
}
async function askStrategy() {
  const { data } = await api.post('/review/strategy', q)
  strategy.value = data
}
async function loadLive() {
  const { data } = await api.get('/review/live/alerts')
  liveAlerts.value = data
}
async function reviewAlert(row) {
  const { data } = await api.post(`/review/live/alerts/${row.alert_id}`)
  liveResult.value = data
}
async function reviewInterval() {
  if (!interval.start || !interval.end) {
    ElMessage.warning('请选择区间开始与结束')
    return
  }
  const body = {
    start: new Date(interval.start).toISOString().slice(0, 19),
    end: new Date(interval.end).toISOString().slice(0, 19),
  }
  const { data } = await api.post('/review/live/interval', body)
  liveResult.value = data
  ElMessage.success('区间复盘已签发')
}
async function drill(refStr) {
  const id = Number(String(refStr).split(':')[1])
  drawer.value = true
  evidence.value = null
  const { data } = await api.get(`/evidence/${id}`)
  evidence.value = data
}

onMounted(() => {
  loadConfig()
  loadList()
  loadLive()
})
</script>

<style scoped>
.mb { margin-bottom: 12px; }
.mt { margin-top: 12px; }
.ml { margin-left: 8px; }
.head { display: flex; justify-content: space-between; align-items: center; }
.hint { color: #909399; font-size: 12px; }
.muted { color: #909399; }
.sc-card { height: 100%; }
.sc-title { font-weight: 600; margin-bottom: 6px; }
.sc-desc { color: #606266; font-size: 12px; min-height: 54px; }
.sc-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.actions { display: flex; gap: 8px; align-items: center; }
.cand { display: inline-block; margin-right: 8px; color: #909399; font-size: 12px; }
.cand.picked { color: #f56c6c; font-weight: 600; }
.tl { margin-top: 16px; max-height: 360px; overflow: auto; }
.cf-card { height: 100%; }
.cf-name { font-weight: 600; margin-bottom: 8px; }
.cf-nums { display: flex; gap: 24px; margin-bottom: 8px; }
.cf-nums .k { color: #909399; font-size: 12px; display: block; }
.cf-nums .v { font-size: 18px; font-weight: 600; }
.cf-nums .v.ok { color: #67c23a; }
.cf-nums .v.warn { color: #e6a23c; }
.cf-explain { color: #606266; font-size: 13px; }
.live-result { margin-top: 12px; }
</style>

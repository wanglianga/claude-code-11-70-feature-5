import os

# 数据库连接：默认使用容器内置 PostgreSQL（ccdev/ccdev@127.0.0.1:5432/ccdev）
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev",
)

# 会话令牌签名密钥（演示环境默认值，生产应通过环境变量覆盖）
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
TOKEN_TTL_HOURS = int(os.getenv("TOKEN_TTL_HOURS", "24"))

# 网关接入令牌（X-Ingest-Token）
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "ingest-dev-token")

# 数据库写入延迟高风险阈值（毫秒）：超过则调度器进入降级模式
WRITE_LATENCY_THRESHOLD_MS = int(os.getenv("WRITE_LATENCY_THRESHOLD_MS", "800"))

# 告警延迟目标（分钟）：值班员需在该时间内看到高风险超标
ALERT_LATENCY_TARGET_MIN = int(os.getenv("ALERT_LATENCY_TARGET_MIN", "15"))

# 实时/补传分流阈值（秒）：读数时间早于当前该时长即视为补传，交由恢复编排器回补
BACKFILL_AGE_SEC = int(os.getenv("BACKFILL_AGE_SEC", "600"))

# 调度器每 tick 单分区批量上限 / 全局上限
BATCH_MAX_ITEMS = int(os.getenv("BATCH_MAX_ITEMS", "50"))
SCHED_TICK_LIMIT = int(os.getenv("SCHED_TICK_LIMIT", "200"))

# 恢复编排器每次运行单分区回补上限
RECOVERY_MAX_ITEMS = int(os.getenv("RECOVERY_MAX_ITEMS", "500"))

# 容量决策复盘：
#   台账保留模式 —— journal(逐次队列比较+评分明细，查询最快但写入量大)
#                | checkpoint(周期检查点+不可变事件序列，日常成本低，长区间需更多重放计算)
REVIEW_LEDGER_MODE = os.getenv("REVIEW_LEDGER_MODE", "checkpoint")
# 复盘响应目标（秒）：影响策略建议中重建精度与成本的取舍
REVIEW_RESPONSE_TARGET_SEC = int(os.getenv("REVIEW_RESPONSE_TARGET_SEC", "5"))
# 周期检查点间隔（秒）
REVIEW_CHECKPOINT_INTERVAL_SEC = int(os.getenv("REVIEW_CHECKPOINT_INTERVAL_SEC", "300"))
# 峰值事件量假设（事件/秒），用于成本边界估算
REVIEW_PEAK_EVENT_RATE = int(os.getenv("REVIEW_PEAK_EVENT_RATE", "50"))
# 监管保留期（天）
REVIEW_RETENTION_DAYS = int(os.getenv("REVIEW_RETENTION_DAYS", "365"))

# 前端构建产物目录（存在时由后端直接托管，便于单进程本地运行）
FRONTEND_DIST = os.getenv(
    "FRONTEND_DIST",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "frontend", "dist"),
)

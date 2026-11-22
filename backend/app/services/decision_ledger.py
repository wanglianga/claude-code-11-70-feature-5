"""容量决策复盘：事件台账、顺序规范器、状态保全（检查点）与成本/精度策略建议。

三条不变量：

1. **不可变事件序列**：所有决策相关事实（接收、批次生成、策略生效、批次完成、
   队列比较、检查点）只追加写入 ``decision_events``，从不原地修改。
2. **预登记序号裁定同刻先后**：同一生效时刻（effective_ts 相同）的事件，**不按
   数据库写入先后**，一律按登记时派发的 ``event_seq`` 大小排序。这是顺序规范器
   的精确定义，用于策略切换与批次完成同一时刻抵达时的复现。
3. **已签发结论不回溯改写**：复盘结论写入 ``review_runs``，补传晚到只生成新版本。

两种台账保留模式（按复盘响应时间/峰值事件量/监管保留期取舍）：

* ``journal``  —— 保存每一次队列比较与评分明细，查询时可直接解释决定，写入量大；
* ``checkpoint`` —— 周期检查点 + 不可变事件序列，日常成本低，长区间复盘需重放计算。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime

from sqlalchemy import func

from .. import config
from ..models import DecisionEvent, QueueCheckpoint

# event_seq 派发锁：保证同一进程中派发序号严格递增（跨时刻与同刻都稳定）。
_seq_lock = threading.Lock()
_last_seq = [0]

LIVE = "live"


def lane_for_scenario(scenario_id: str | None) -> str:
    return LIVE if not scenario_id else f"scenario:{scenario_id}"


def is_scenario_lane(lane: str) -> bool:
    return lane.startswith("scenario:")


def scenario_of_lane(lane: str) -> str | None:
    return lane.split(":", 1)[1] if is_scenario_lane(lane) else None


def next_event_seq(db) -> int:
    """派发下一个全局事件序号（严格递增，登记时刻即确定）。"""
    with _seq_lock:
        if _last_seq[0] == 0:
            top = db.query(func.max(DecisionEvent.event_seq)).scalar() or 0
            _last_seq[0] = int(top)
        _last_seq[0] += 1
        return _last_seq[0]


def record_event(
    db,
    kind: str,
    effective_ts: datetime,
    *,
    lane: str = LIVE,
    partition_key: str | None = None,
    receive_log_id: int | None = None,
    batch_id: int | None = None,
    policy_version: int | None = None,
    payload: dict | None = None,
    actor: str = "system",
    note: str = "",
    event_seq: int | None = None,
) -> DecisionEvent:
    """追加一条不可变决策事件（不 commit，由调用方控制事务边界）。"""
    ev = DecisionEvent(
        lane=lane,
        event_seq=event_seq if event_seq is not None else next_event_seq(db),
        kind=kind,
        effective_ts=effective_ts,
        actor=actor,
        partition_key=partition_key,
        receive_log_id=receive_log_id,
        batch_id=batch_id,
        policy_version=policy_version,
        payload=payload or {},
        note=note,
    )
    db.add(ev)
    return ev


# ---------------------------------------------------------------------------
# 顺序规范器
# ---------------------------------------------------------------------------

KIND_RANK = {
    # 同一 event_seq 理论上不会重复；kind 仅作同刻同序时的兜底稳定键。
    "receive": 0,
    "policy_effective": 1,
    "batch_create": 2,
    "decision": 3,
    "batch_complete": 4,
    "late_arrival": 5,
    "checkpoint": 9,
}


def normalize_order(events: list[DecisionEvent]) -> list[DecisionEvent]:
    """顺序规范器：生效时刻优先，同刻按预登记 event_seq 裁定，绝不依赖写入先后。"""
    return sorted(
        events,
        key=lambda e: (e.effective_ts, int(e.event_seq or 0), KIND_RANK.get(e.kind, 8), e.id or 0),
    )


def effective_seq_map(db, lane: str) -> dict[int, int]:
    """策略版本 -> 生效事件序号（策略生效与其它事件同刻时用此裁定）。"""
    rows = (
        db.query(DecisionEvent.policy_version, DecisionEvent.event_seq)
        .filter(DecisionEvent.lane == lane, DecisionEvent.kind == "policy_effective")
        .all()
    )
    return {int(v): int(s) for v, s in rows if v is not None}


# ---------------------------------------------------------------------------
# 状态保全：检查点
# ---------------------------------------------------------------------------

def snapshot_partitions(db, lane: str) -> dict:
    """按复现通道汇总分区水位与游标（live 与 scenario 各自独立口径）。"""
    from ..models import BatchItem, Partition, QualityConclusion, ReceiveLog

    lane_filter = {"scenario_id": None} if lane == LIVE else {"scenario_id": scenario_of_lane(lane)}
    confirmed = dict(
        db.query(ReceiveLog.partition_key, func.count(ReceiveLog.id))
        .filter(ReceiveLog.status == "confirmed", *[getattr(ReceiveLog, k) == v for k, v in lane_filter.items()])
        .group_by(ReceiveLog.partition_key)
        .all()
    )
    batched = dict(
        db.query(ReceiveLog.partition_key, func.count(BatchItem.id))
        .join(ReceiveLog, BatchItem.receive_log_id == ReceiveLog.id)
        .filter(*[getattr(ReceiveLog, k) == v for k, v in lane_filter.items()])
        .group_by(ReceiveLog.partition_key)
        .all()
    )
    processed = dict(
        db.query(QualityConclusion.partition_key, func.count(QualityConclusion.id))
        .filter(*[getattr(QualityConclusion, k) == v for k, v in lane_filter.items()])
        .group_by(QualityConclusion.partition_key)
        .all()
    )
    rows = {}
    for p in db.query(Partition).order_by(Partition.partition_key).all():
        c = confirmed.get(p.partition_key, 0)
        b = batched.get(p.partition_key, 0)
        pr = processed.get(p.partition_key, 0)
        rows[p.partition_key] = {
            "risk_class": p.risk_class,
            "confirmed": c,
            "batched": b,
            "processed": pr,
            "backlog": c - b,
            "inflight": b - pr,
            "confirmed_cursor": p.confirmed_cursor,
            "batched_cursor": p.batched_cursor,
            "processed_cursor": p.processed_cursor,
        }
    return rows


def write_checkpoint(db, lane: str, effective_ts: datetime | None = None, note: str = "") -> QueueCheckpoint:
    """保存可供回溯的队列水位与调度游标快照。"""
    from ..models import Policy, SchedulerState

    st = db.get(SchedulerState, 1)
    active = db.query(Policy).filter(Policy.active.is_(True)).first()
    last_seq = (
        db.query(func.max(DecisionEvent.event_seq))
        .filter(DecisionEvent.lane == lane)
        .scalar()
        or 0
    )
    sched_cursor = (
        db.query(func.max(DecisionEvent.receive_log_id))
        .filter(DecisionEvent.lane == lane, DecisionEvent.kind == "batch_create")
        .scalar()
        or 0
    )
    cp = QueueCheckpoint(
        lane=lane,
        checkpoint_seq=int(last_seq),
        effective_ts=effective_ts or datetime.utcnow(),
        degraded=bool(st.degraded) if st else False,
        policy_version=active.version if active else None,
        partitions_json=snapshot_partitions(db, lane),
        scheduler_cursor=int(sched_cursor),
        detail={"note": note},
    )
    db.add(cp)
    return cp


def latest_checkpoint(db, lane: str, at_or_before: datetime | None = None) -> QueueCheckpoint | None:
    q = db.query(QueueCheckpoint).filter(QueueCheckpoint.lane == lane)
    if at_or_before is not None:
        q = q.filter(QueueCheckpoint.effective_ts <= at_or_before)
    return q.order_by(QueueCheckpoint.effective_ts.desc(), QueueCheckpoint.id.desc()).first()


# ---------------------------------------------------------------------------
# 台账保留模式与成本/精度边界
# ---------------------------------------------------------------------------

MODES = {
    "journal": {
        "label": "逐次队列比较日志（journal）",
        "precision": "event_grade",
        "desc": "保存每一次队列比较与评分明细，查询时可直接解释决定；持续写入量很大。",
    },
    "checkpoint": {
        "label": "周期检查点 + 不可变事件序列（checkpoint）",
        "precision": "checkpoint_grade",
        "desc": "日常仅追加不可变事件与周期检查点，写入成本低；长区间复盘需按事件序列重放计算。",
    },
}


def mode_info(mode: str | None = None) -> dict:
    mode = (mode or config.REVIEW_LEDGER_MODE or "checkpoint").lower()
    if mode not in MODES:
        mode = "checkpoint"
    return {"mode": mode, **MODES[mode], "interval_sec": config.REVIEW_CHECKPOINT_INTERVAL_SEC}


def cost_profile() -> dict:
    """成本边界估算：按峰值事件量与保留期推算两种模式的写入规模与重放计算量。"""
    peak = config.REVIEW_PEAK_EVENT_RATE
    retention = config.REVIEW_RETENTION_DAYS
    interval = max(config.REVIEW_CHECKPOINT_INTERVAL_SEC, 1)
    # journal：每 tick 一次队列比较（含评分明细）近似等于事件量本身；
    #           额外估算每条比较明细约 0.6KB。
    journal_events_day = peak * 86400
    journal_kb_day = journal_events_day * 0.6
    # checkpoint：不可变事件(约 0.25KB/条) + 每 interval 一个检查点(约 4KB)
    checkpoint_events_day = peak * 86400
    checkpoint_kb_day = checkpoint_events_day * 0.25 + (86400 / interval) * 4
    # 重放计算量：checkpoint 模式下，区间越长，需折叠的事件越多（近似线性）。
    replay_per_hour_sec = round(peak * 3600 / 4000.0, 3)  # 4000 events/s 的折叠吞吐估算
    return {
        "peak_event_rate": peak,
        "retention_days": retention,
        "response_target_sec": config.REVIEW_RESPONSE_TARGET_SEC,
        "checkpoint_interval_sec": interval,
        "journal": {
            "mode": "journal",
            "writes_per_day": journal_events_day,
            "storage_mb_per_day": round(journal_kb_day / 1024, 1),
            "storage_gb_per_year": round(journal_kb_day * 365 / 1024 / 1024, 2),
            "query_explanation": "可直接读取当时每次比较与评分，无需重放",
            "replay_cost_sec_per_query": 0.0,
        },
        "checkpoint": {
            "mode": "checkpoint",
            "writes_per_day": checkpoint_events_day + int(86400 / interval),
            "storage_mb_per_day": round(checkpoint_kb_day / 1024, 1),
            "storage_gb_per_year": round(checkpoint_kb_day * 365 / 1024 / 1024, 2),
            "query_explanation": "从最近检查点起重放区间内事件以重建决策",
            "replay_cost_sec_per_hour_window": replay_per_hour_sec,
        },
    }


def recommend_mode(
    response_target_sec: float | None = None,
    peak_event_rate: int | None = None,
    retention_days: int | None = None,
) -> dict:
    """按可接受复盘响应时间、峰值事件量、监管保留期给出模式建议与理由。"""
    response_target_sec = response_target_sec or config.REVIEW_RESPONSE_TARGET_SEC
    peak_event_rate = peak_event_rate or config.REVIEW_PEAK_EVENT_RATE
    retention_days = retention_days or config.REVIEW_RETENTION_DAYS
    interval = max(config.REVIEW_CHECKPOINT_INTERVAL_SEC, 1)
    # 单次查询在 checkpoint 模式下最坏需要重放一个检查点间隔的事件量。
    checkpoint_replay_events = peak_event_rate * interval
    checkpoint_query_sec = round(checkpoint_replay_events / 4000.0, 3)
    journal_storage_gb = round(peak_event_rate * 86400 * 0.6 * retention_days / 1024 / 1024, 2)
    checkpoint_storage_gb = round(
        (peak_event_rate * 86400 * 0.25 + (86400 / interval) * 4) * retention_days / 1024 / 1024, 2
    )

    reasons = []
    if response_target_sec < 1.0 and peak_event_rate * interval > 20000:
        choice = "journal"
        reasons.append(
            f"响应目标 {response_target_sec}s 很紧，且单检查点间隔事件量约 {checkpoint_replay_events}，"
            "重放可能超出预算，改用逐次比较日志直接解释"
        )
    elif journal_storage_gb > 200 and peak_event_rate > 100:
        choice = "checkpoint"
        reasons.append(
            f"峰值 {peak_event_rate} 事件/秒且保留 {retention_days} 天，逐次比较日志约需 "
            f"{journal_storage_gb}GB，写入与留存成本过高，改用检查点+事件序列"
        )
    else:
        choice = "checkpoint"
        reasons.append(
            f"检查点模式单次查询重放约 {checkpoint_query_sec}s（目标 {response_target_sec}s 内），"
            f"留存 {retention_days} 天约 {checkpoint_storage_gb}GB，日常成本更低"
        )
    if retention_days >= 180:
        reasons.append("监管保留期较长，优先压低日常写入量，长区间仍可凭事件序列完整重放")
    if peak_event_rate >= 100:
        reasons.append("峰值事件量高，检查点写入放大远小于逐次比较")

    info = MODES[choice]
    return {
        "recommended_mode": choice,
        "label": info["label"],
        "precision": info["precision"],
        "reasons": reasons,
        "inputs": {
            "response_target_sec": response_target_sec,
            "peak_event_rate": peak_event_rate,
            "retention_days": retention_days,
            "checkpoint_interval_sec": interval,
        },
        "estimates": {
            "checkpoint_query_sec": checkpoint_query_sec,
            "journal_storage_gb": journal_storage_gb,
            "checkpoint_storage_gb": checkpoint_storage_gb,
        },
    }


def content_hash(data) -> str:
    import json

    return hashlib.sha256(json.dumps(to_jsonable(data), sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:24]


def to_jsonable(obj):
    """递归把 datetime 等非 JSON 原生类型转成字符串，保证 JSON 列可持久化。"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj

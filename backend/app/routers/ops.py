from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import (
    Alert,
    Batch,
    BatchItem,
    Device,
    GatewaySession,
    Partition,
    QualityConclusion,
    ReadingConflict,
    ReceiveLog,
    RecoveryRun,
    User,
    WriteMetric,
)
from ..schemas import RecoveryRunIn
from ..security import require_user
from ..services import recovery as recovery_svc
from ..services.common import get_state

router = APIRouter(prefix="/api", tags=["ops"])


def _counts_by_partition(db, status_filter=None):
    q = db.query(ReceiveLog.partition_key, func.count(ReceiveLog.id)).filter(
        ReceiveLog.scenario_id.is_(None)  # 生产口径排除复盘模拟通道
    )
    if status_filter:
        q = q.filter(ReceiveLog.status == status_filter)
    return dict(q.group_by(ReceiveLog.partition_key).all())


@router.get("/partitions")
def list_partitions(db: Session = Depends(get_db), _: User = Depends(require_user)):
    """分区积压水位：已确认/已入批/已处理 游标与数量、缺口。"""
    confirmed = _counts_by_partition(db, "confirmed")
    batched = dict(
        db.query(ReceiveLog.partition_key, func.count(BatchItem.id))
        .join(BatchItem, BatchItem.receive_log_id == ReceiveLog.id)
        .filter(ReceiveLog.scenario_id.is_(None))
        .group_by(ReceiveLog.partition_key)
        .all()
    )
    processed = dict(
        db.query(QualityConclusion.partition_key, func.count(QualityConclusion.id))
        .filter(QualityConclusion.scenario_id.is_(None))
        .group_by(QualityConclusion.partition_key)
        .all()
    )
    rows = []
    for p in db.query(Partition).order_by(Partition.partition_key).all():
        c = confirmed.get(p.partition_key, 0)
        b = batched.get(p.partition_key, 0)
        pr = processed.get(p.partition_key, 0)
        rows.append(
            {
                "partition_key": p.partition_key,
                "risk_class": p.risk_class,
                "description": p.description,
                "confirmed_cursor": p.confirmed_cursor,
                "batched_cursor": p.batched_cursor,
                "processed_cursor": p.processed_cursor,
                "confirmed_count": c,
                "batched_count": b,
                "processed_count": pr,
                "backlog": c - b,  # 已确认未入批 = 积压水位
                "inflight": b - pr,  # 已入批未处理
                "consistent": c == b == pr,
            }
        )
    return rows


@router.get("/partitions/integrity")
def partitions_integrity(db: Session = Depends(get_db), _: User = Depends(require_user)):
    """最终分区完整性：每个分区的 已确认 == 已入批 == 已处理。"""
    rows = list_partitions(db, _)
    return {
        "consistent": all(r["consistent"] for r in rows),
        "partitions": rows,
    }


@router.get("/recovery/runs")
def list_recovery_runs(db: Session = Depends(get_db), _: User = Depends(require_user)):
    rows = db.query(RecoveryRun).order_by(RecoveryRun.id.desc()).limit(100).all()
    return [
        {
            "id": r.id,
            "partition_key": r.partition_key,
            "trigger": r.trigger,
            "from_cursor": r.from_cursor,
            "to_cursor": r.to_cursor,
            "recovered_count": r.recovered_count,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in rows
    ]


@router.post("/recovery/run")
def run_recovery(body: RecoveryRunIn, db: Session = Depends(get_db), _: User = Depends(require_user)):
    runs = recovery_svc.recovery_tick(db, manual_partition=body.partition_key, trigger="manual")
    return {"runs": runs, "count": len(runs)}


@router.get("/scheduler/state")
def scheduler_state(db: Session = Depends(get_db), _: User = Depends(require_user)):
    st = get_state(db)
    from ..models import Policy

    active = db.query(Policy).filter(Policy.active.is_(True)).first()
    return {
        "degraded": st.degraded,
        "low_risk_paused": st.low_risk_paused,
        "threshold_ms": st.threshold_ms,
        "last_latency_ms": st.last_latency_ms,
        "trends_paused_count": st.trends_paused_count,
        "active_policy_version": active.version if active else None,
        "reserve_high_writes": active.reserve_high_writes if active else None,
        "updated_at": st.updated_at.isoformat() if st.updated_at else None,
    }


@router.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db), _: User = Depends(require_user)):
    total = db.query(ReceiveLog).filter(ReceiveLog.scenario_id.is_(None)).count()
    by_status = dict(
        db.query(ReceiveLog.status, func.count(ReceiveLog.id))
        .filter(ReceiveLog.scenario_id.is_(None))
        .group_by(ReceiveLog.status)
        .all()
    )
    open_alerts = db.query(Alert).filter(Alert.status == "open").count()
    max_latency = db.query(func.max(Alert.latency_ms)).scalar() or 0
    avg_latency = db.query(func.avg(Alert.latency_ms)).scalar() or 0
    conflicts = db.query(ReadingConflict).count()
    conclusions = db.query(QualityConclusion).filter(QualityConclusion.scenario_id.is_(None)).count()
    pending_review = (
        db.query(QualityConclusion)
        .filter(QualityConclusion.conclusion == "pending_review", QualityConclusion.scenario_id.is_(None))
        .count()
    )
    st = get_state(db)
    return {
        "total_received": total,
        "confirmed": by_status.get("confirmed", 0),
        "duplicate": by_status.get("duplicate", 0),
        "conflict": by_status.get("conflict", 0),
        "conflict_records": conflicts,
        "conclusions": conclusions,
        "pending_review": pending_review,
        "open_alerts": open_alerts,
        "max_alert_latency_ms": int(max_latency),
        "avg_alert_latency_ms": int(avg_latency),
        "alert_latency_target_ms": config.ALERT_LATENCY_TARGET_MIN * 60 * 1000,
        "degraded": st.degraded,
        "last_write_latency_ms": st.last_latency_ms,
        "write_latency_threshold_ms": st.threshold_ms,
    }


@router.get("/dashboard/write-metrics")
def write_metrics(limit: int = 60, db: Session = Depends(get_db), _: User = Depends(require_user)):
    rows = db.query(WriteMetric).order_by(WriteMetric.id.desc()).limit(limit).all()
    rows.reverse()
    return [
        {"id": m.id, "latency_ms": m.latency_ms, "created_at": m.created_at.isoformat() if m.created_at else None}
        for m in rows
    ]


@router.get("/quality/conclusions")
def list_conclusions(
    conclusion: str | None = None,
    device_point: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    _: User = Depends(require_user),
):
    q = db.query(QualityConclusion).filter(QualityConclusion.scenario_id.is_(None))
    if conclusion:
        q = q.filter(QualityConclusion.conclusion == conclusion)
    if device_point:
        q = q.filter(QualityConclusion.device_point == device_point)
    total = q.count()
    rows = q.order_by(QualityConclusion.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "items": [
            {
                "id": c.id,
                "receive_log_id": c.receive_log_id,
                "batch_id": c.batch_id,
                "device_point": c.device_point,
                "partition_key": c.partition_key,
                "policy_version": c.policy_version,
                "range_ok": c.range_ok,
                "ts_ok": c.ts_ok,
                "device_status_ok": c.device_status_ok,
                "gap_detected": c.gap_detected,
                "conclusion": c.conclusion,
                "confirmed": c.confirmed,
                "detail": c.detail,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ],
    }


@router.get("/evidence/{log_id}")
def evidence_chain(log_id: int, db: Session = Depends(get_db), _: User = Depends(require_user)):
    """证据链：接收日志 → 网关会话 → 设备 → 批次 → 质量结论 → 告警 → 冲突证据。"""
    log = db.get(ReceiveLog, log_id)
    if log is None:
        raise HTTPException(404, "接收日志不存在")
    session = db.query(GatewaySession).filter(GatewaySession.session_uuid == log.session_uuid).first()
    device = db.query(Device).filter(Device.point_code == log.device_point).first()
    item = db.query(BatchItem).filter(BatchItem.receive_log_id == log.id).first()
    batch = db.get(Batch, item.batch_id) if item else None
    conclusion = db.query(QualityConclusion).filter(QualityConclusion.receive_log_id == log.id).first()
    alert = db.query(Alert).filter(Alert.receive_log_id == log.id).first()
    conflicts_as_source = db.query(ReadingConflict).filter(ReadingConflict.confirmed_log_id == log.id).all()
    conflict_as_late = db.query(ReadingConflict).filter(ReadingConflict.conflict_log_id == log.id).first()
    conflict_logs = []
    for c in conflicts_as_source:
        cl = db.get(ReceiveLog, c.conflict_log_id)
        if cl:
            conflict_logs.append({"log_id": cl.id, "value": cl.value, "fingerprint": cl.fingerprint,
                                  "received_at": cl.received_at.isoformat()})
    return {
        "receive_log": {
            "id": log.id,
            "gateway_code": log.gateway_code,
            "session_uuid": log.session_uuid,
            "source_seq": log.source_seq,
            "fingerprint": log.fingerprint,
            "device_point": log.device_point,
            "reading_ts": log.reading_ts.isoformat(),
            "value": log.value,
            "status": log.status,
            "note": log.note,
            "received_at": log.received_at.isoformat(),
            "raw_payload": log.raw_payload,
        },
        "session": {
            "gateway_code": session.gateway_code,
            "session_uuid": session.session_uuid,
            "started_at": session.started_at.isoformat(),
            "status": session.status,
        }
        if session
        else None,
        "device": {
            "point_code": device.point_code,
            "name": device.name,
            "factor": device.factor,
            "risk_level": device.risk_level,
            "partition_key": device.partition_key,
            "threshold_min": device.threshold_min,
            "threshold_max": device.threshold_max,
        }
        if device
        else None,
        "batch": {
            "id": batch.id,
            "policy_version": batch.policy_version,
            "source": batch.source,
            "status": batch.status,
            "recovery_run_id": batch.recovery_run_id,
        }
        if batch
        else None,
        "conclusion": {
            "id": conclusion.id,
            "conclusion": conclusion.conclusion,
            "confirmed": conclusion.confirmed,
            "range_ok": conclusion.range_ok,
            "ts_ok": conclusion.ts_ok,
            "device_status_ok": conclusion.device_status_ok,
            "policy_version": conclusion.policy_version,
            "detail": conclusion.detail,
        }
        if conclusion
        else None,
        "alert": {
            "id": alert.id,
            "level": alert.level,
            "status": alert.status,
            "latency_ms": alert.latency_ms,
            "message": alert.message,
        }
        if alert
        else None,
        "conflicts": conflict_logs,
        "is_conflict_entry": conflict_as_late is not None,
        "conflict_kept_log_id": conflict_as_late.confirmed_log_id if conflict_as_late else None,
    }

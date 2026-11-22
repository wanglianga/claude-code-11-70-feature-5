from datetime import datetime, timedelta

from sqlalchemy import case

from .. import config
from ..models import (
    Batch,
    BatchItem,
    DerivedTrend,
    Device,
    Partition,
    QualityConclusion,
    ReceiveLog,
)
from . import alerts
from . import decision_ledger as ledger
from .common import get_state


def evaluate(db, log: ReceiveLog, device: Device | None, batch: Batch) -> QualityConclusion:
    """质量服务：范围 / 时序 / 设备状态三项检查，输出结论。"""
    range_ok = True
    if device is not None:
        if device.threshold_min is not None and log.value < device.threshold_min:
            range_ok = False
        if device.threshold_max is not None and log.value > device.threshold_max:
            range_ok = False
    # 时序检查：读数时间不应晚于接收时间（容忍 5 分钟时钟偏差）
    ts_ok = log.reading_ts <= log.received_at + timedelta(minutes=5)
    device_ok = (log.device_status_at_receipt or "normal") == "normal"

    # 采样间隔缺口检查（信息项，不改变结论）
    gap = False
    if device is not None:
        prev = (
            db.query(ReceiveLog)
            .filter(
                ReceiveLog.device_point == log.device_point,
                ReceiveLog.status == "confirmed",
                ReceiveLog.reading_ts < log.reading_ts,
            )
            .order_by(ReceiveLog.reading_ts.desc())
            .first()
        )
        if prev is not None:
            gap = (log.reading_ts - prev.reading_ts).total_seconds() > 2 * device.sampling_period_sec

    if not device_ok:
        # 设备处于校准/维护：保留来源与待核结论，不作为正式超标
        conclusion, confirmed = "pending_review", False
        detail = f"设备接收时状态为 {log.device_status_at_receipt}，保留来源，待人工复核"
    elif not ts_ok:
        conclusion, confirmed = "pending_review", False
        detail = "时序异常：读数时间晚于接收时间，待人工复核"
    elif not range_ok:
        conclusion, confirmed = "exceed", True
        detail = f"数值 {log.value} 超出阈值 [{device.threshold_min}, {device.threshold_max}]"
    else:
        conclusion, confirmed = "normal", True
        detail = "范围/时序/设备状态检查通过"

    return QualityConclusion(
        receive_log_id=log.id,
        batch_id=batch.id,
        device_point=log.device_point,
        partition_key=log.partition_key,
        policy_version=batch.policy_version,  # 结论引用批次创建时的策略版本
        range_ok=range_ok,
        ts_ok=ts_ok,
        device_status_ok=device_ok,
        gap_detected=gap,
        conclusion=conclusion,
        confirmed=confirmed,
        detail=detail,
    )


def quality_tick(db) -> bool:
    """处理一个待处理批次（高风险分区优先）。返回是否处理了批次。"""
    state = get_state(db)
    batch = (
        db.query(Batch)
        .filter(Batch.status == "open")
        .join(Partition, Batch.partition_key == Partition.partition_key)
        .order_by(case((Partition.risk_class == "high", 0), else_=1), Batch.id)
        .first()
    )
    if batch is None:
        return False
    batch.status = "processing"
    db.commit()

    rows = (
        db.query(BatchItem, ReceiveLog)
        .join(ReceiveLog, BatchItem.receive_log_id == ReceiveLog.id)
        .filter(BatchItem.batch_id == batch.id)
        .order_by(ReceiveLog.id)
        .all()
    )
    devices = {d.point_code: d for d in db.query(Device).all()}
    max_log_id = 0
    for item, log in rows:
        device = devices.get(log.device_point)
        conclusion = evaluate(db, log, device, batch)
        db.add(conclusion)
        db.flush()
        alerts.maybe_alert(db, conclusion, log, device)
        # 低风险衍生计算（滚动均值）：降级模式下暂停，把写入额度让给高风险分区
        if device is not None and device.risk_level == "low":
            if state.degraded:
                state.trends_paused_count = (state.trends_paused_count or 0) + 1
            else:
                window = log.reading_ts.replace(minute=0, second=0, microsecond=0)
                db.add(
                    DerivedTrend(
                        device_point=log.device_point,
                        window_start=window,
                        avg_value=log.value,
                        sample_count=1,
                    )
                )
        max_log_id = max(max_log_id, log.id)

    batch.status = "done"
    batch.processed_at = datetime.utcnow()
    part = db.get(Partition, batch.partition_key)
    if part is not None and max_log_id > (part.processed_cursor or 0):
        part.processed_cursor = max_log_id
    # 批次完成是不可变事件；其生效时刻与策略切换同刻时，由预登记序号裁定先后。
    if batch.scenario_id is None:
        ledger.record_event(
            db,
            "batch_complete",
            batch.processed_at,
            lane=ledger.LIVE,
            partition_key=batch.partition_key,
            batch_id=batch.id,
            policy_version=batch.policy_version,
            actor="quality",
            payload={"item_count": batch.item_count, "source": batch.source},
            note=f"批次 #{batch.id} 完成",
        )
        if ledger.mode_info()["mode"] == "checkpoint":
            _maybe_checkpoint(db, lane=ledger.LIVE)
    db.commit()
    return True


_cp_timer = [0.0]


def _maybe_checkpoint(db, lane: str) -> None:
    """周期检查点：降低日常写入量；长区间复盘以此作为重放起点。"""
    import time

    now = time.time()
    if now - _cp_timer[0] < config.REVIEW_CHECKPOINT_INTERVAL_SEC:
        return
    _cp_timer[0] = now
    ledger.write_checkpoint(db, lane, note="周期检查点（批次完成触发）")

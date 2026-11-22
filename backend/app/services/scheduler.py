from datetime import datetime, timedelta

from .. import config
from ..models import Batch, BatchItem, Partition, Policy, ReceiveLog
from . import decision_ledger as ledger
from .common import batch_lock, get_state


def active_policy(db) -> Policy | None:
    return db.query(Policy).filter(Policy.active.is_(True)).first()


def unbatched_query(db):
    """已确认但尚未放入任何批次的接收日志。

    排除复盘模拟通道（scenario_id 非空）：模拟序列只供复盘服务读取，
    绝不参与真实调度，避免污染生产台账与完整性口径。
    """
    subq = db.query(BatchItem.receive_log_id)
    return db.query(ReceiveLog).filter(
        ReceiveLog.status == "confirmed",
        ReceiveLog.scenario_id.is_(None),
        ~ReceiveLog.id.in_(subq),
    )


def make_batches(db, entries, policy: Policy, source: str, recovery_run_id=None) -> int:
    """把接收日志按分区分组入批；批次在创建时固化当前策略版本。调用方须持有 batch_lock。

    每一步入批都登记一条不可变 ``batch_create`` 事件（含候选分区与选定分区的评分明细），
    供复盘引擎重建“可选批次”与“实际选择”。
    """
    by_part: dict[str, list] = {}
    for e in entries:
        by_part.setdefault(e.partition_key, []).append(e)
    made = 0
    weights = policy.priority_json or {}
    parts = {p.partition_key: p for p in db.query(Partition).all()}
    degraded = bool(get_state(db).degraded)

    def _weight(pkey):
        p = parts.get(pkey)
        return int(weights.get(p.risk_class, 0)) if p else 0

    # 每个分区此刻待入批的积压量，作为该步的候选评分明细
    candidates = [
        {
            "partition_key": pkey,
            "risk_class": parts[pkey].risk_class if pkey in parts else None,
            "weight": _weight(pkey),
            "backlog": len(items),
        }
        for pkey, items in by_part.items()
    ]
    candidates.sort(key=lambda c: (-c["weight"], c["partition_key"]))

    for pkey, items in by_part.items():
        for i in range(0, len(items), config.BATCH_MAX_ITEMS):
            chunk = items[i : i + config.BATCH_MAX_ITEMS]
            batch = Batch(
                partition_key=pkey,
                policy_version=policy.version,  # 版本在创建时固化，之后策略升级不回溯改写
                source=source,
                recovery_run_id=recovery_run_id,
                item_count=len(chunk),
            )
            db.add(batch)
            db.flush()
            for e in chunk:
                db.add(BatchItem(batch_id=batch.id, receive_log_id=e.id))
            ev = ledger.record_event(
                db,
                "batch_create",
                batch.created_at or datetime.utcnow(),
                partition_key=pkey,
                batch_id=batch.id,
                receive_log_id=max(e.id for e in chunk),
                policy_version=policy.version,
                actor="scheduler",
                payload={
                    "candidates": candidates,
                    "selected": pkey,
                    "degraded": degraded,
                    "source": source,
                    "explain": (
                        f"按策略 v{policy.version} 权重选定 {pkey}（权重 {_weight(pkey)}，"
                        f"积压 {len(chunk)} 条）"
                        + ("；降级中仅高风险分区分配额度" if degraded else "")
                    ),
                    "cost_note": "通道单槽，逐分区串行入批",
                },
                note=f"实时通道入批 {pkey}",
            )
            batch.create_event_seq = ev.event_seq
            made += 1
        part = db.get(Partition, pkey)
        max_id = max(e.id for e in items)
        if part is not None and max_id > (part.batched_cursor or 0):
            part.batched_cursor = max_id
    return made


def scheduler_tick(db) -> int:
    """实时通道：只接近期读数；降级模式下只为高风险分区分配写入额度。"""
    state = get_state(db)
    policy = active_policy(db)
    if policy is None:
        return 0
    cutoff = datetime.utcnow() - timedelta(seconds=config.BACKFILL_AGE_SEC)
    with batch_lock:
        q = unbatched_query(db).filter(ReceiveLog.reading_ts >= cutoff)
        if state.degraded:
            high_parts = [p.partition_key for p in db.query(Partition).filter_by(risk_class="high")]
            q = q.filter(ReceiveLog.partition_key.in_(high_parts))
        entries = q.order_by(ReceiveLog.id).limit(config.SCHED_TICK_LIMIT).all()
        if not entries:
            return 0
        # 按策略权重排序：高优先级分区先入批
        weights = policy.priority_json or {}
        parts = {p.partition_key: p for p in db.query(Partition).all()}
        entries.sort(
            key=lambda e: (
                -int(weights.get(parts[e.partition_key].risk_class, 0)) if e.partition_key in parts else 0,
                e.id,
            )
        )
        made = make_batches(db, entries, policy, source="normal")
        # 记录一次队列比较与评分明细（journal 模式即逐次比较日志；
        # checkpoint 模式仅在检查点落库，日常写入量更低）。
        if ledger.mode_info()["mode"] == "journal":
            ledger.record_event(
                db,
                "decision",
                datetime.utcnow(),
                actor="scheduler",
                payload={
                    "type": "queue_compare",
                    "degraded": state.degraded,
                    "policy_version": policy.version,
                    "batches_made": made,
                    "scores": [
                        {
                            "partition_key": pkey,
                            "risk_class": parts[pkey].risk_class if pkey in parts else None,
                            "weight": int(weights.get(parts[pkey].risk_class, 0)) if pkey in parts else 0,
                            "items": sum(
                                1 for e in entries if e.partition_key == pkey
                            ),
                        }
                        for pkey in sorted({e.partition_key for e in entries})
                    ],
                },
                note="tick 队列比较与评分明细",
            )
        db.commit()
        return made

from datetime import datetime

from .. import config
from ..models import ReceiveLog, RecoveryRun
from .common import batch_lock, get_state
from .scheduler import active_policy, make_batches, unbatched_query


def recovery_tick(db, manual_partition: str | None = None, trigger: str = "auto") -> list[dict]:
    """恢复编排器：按分区游标回补积压（补传读数 + 降级期间被跳过的分区）。

    降级期间自动回补暂停——写入额度优先留给高风险分区的实时通道。
    """
    state = get_state(db)
    if state.degraded and trigger == "auto":
        return []
    policy = active_policy(db)
    if policy is None:
        return []

    runs = []
    with batch_lock:
        q = unbatched_query(db)
        if manual_partition:
            q = q.filter(ReceiveLog.partition_key == manual_partition)
        entries = q.order_by(ReceiveLog.id).limit(5000).all()
        by_part: dict[str, list] = {}
        for e in entries:
            bucket = by_part.setdefault(e.partition_key, [])
            if len(bucket) < config.RECOVERY_MAX_ITEMS:
                bucket.append(e)
        for pkey, items in by_part.items():
            if not items:
                continue
            run = RecoveryRun(
                partition_key=pkey,
                trigger=trigger,
                from_cursor=(items[0].id - 1),
                to_cursor=items[-1].id,
                recovered_count=len(items),
                status="done",
                finished_at=datetime.utcnow(),
            )
            db.add(run)
            db.flush()
            make_batches(db, items, policy, source="recovery", recovery_run_id=run.id)
            runs.append(
                {
                    "run_id": run.id,
                    "partition_key": pkey,
                    "from_cursor": run.from_cursor,
                    "to_cursor": run.to_cursor,
                    "recovered_count": run.recovered_count,
                    "trigger": trigger,
                }
            )
        db.commit()
    return runs

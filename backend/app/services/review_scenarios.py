"""容量决策复盘的三条确定性模拟序列。

每条序列构造一个**隔离通道**（lane = ``scenario:<key>``）的接收日志 / 批次 /
决策事件 / 检查点，然后调用复盘服务签发结论。隔离通道：

* 不参与真实调度与质量消费（``unbatched_query`` 与常规列表均排除 scenario 行）；
* 场景策略通过 ``policy_effective`` 事件携带快照，**不写入生产策略台账**；
* 序列时间为确定性的相对偏移，便于复盘页面稳定复现。

序列一 capacity      —— 容量耗尽
序列二 low_risk      —— 低风险阻塞（降级期间低风险批次先占用单通道）
序列三 policy_race   —— 策略切换竞争（策略切换与批次完成同一生效时刻，按预登记序号裁定）
       insufficient  —— 事件链断裂保护演示（缺少接收事件/检查点时只给“证据不足”）

另提供 ``apply_late_event`` 用于“补传晚到落入已复盘区间 → 新版本 + 差异说明”的演示，
它只追加事件与数据，不改写任何已签发的复盘结论。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func

from ..models import (
    Batch,
    BatchItem,
    DecisionEvent,
    Device,
    Partition,
    QualityConclusion,
    QueueCheckpoint,
    ReceiveLog,
    ReviewRun,
    ScenarioRun,
)
from . import decision_ledger as ledger
from . import review as review_svc


def _lane(key: str) -> str:
    return f"scenario:{key}"


def _risk(db, partition_key: str) -> str:
    row = db.get(Partition, partition_key)
    return row.risk_class if row else "low"


def _device_for(db, partition_key: str, prefer_high: bool = False, value: float | None = None):
    q = db.query(Device).filter(Device.partition_key == partition_key)
    pool = q.all() or db.query(Device).all()
    if prefer_high:
        high = [d for d in pool if d.risk_level == "high"]
        if high:
            pool = high
    if value is not None and len(pool) > 1:
        # 选阈值与读数最匹配的设备，使质量结论与读数数值自洽
        def gap(d):
            hi = d.threshold_max if d.threshold_max is not None else 1e9
            lo = d.threshold_min if d.threshold_min is not None else -1e9
            if value > hi:
                return 0.0
            if value < lo:
                return abs(value - lo)
            return -min(abs(value - hi), abs(value - lo))
        pool = sorted(pool, key=gap)
    return pool[0]


def _make_reading(db, key: str, partition_key: str, received_at: datetime, value: float,
                  prefer_high: bool = True, session_tag: str = "sc") -> ReceiveLog:
    dev = _device_for(db, partition_key, prefer_high=prefer_high, value=value)
    log = ReceiveLog(
        gateway_code="GW-REVIEW",
        session_uuid=f"rev-{key}-{session_tag}-{uuid.uuid4().hex[:6]}",
        source_seq=1,
        fingerprint=f"rev{key}{uuid.uuid4().hex[:8]}",
        device_point=dev.point_code,
        reading_ts=received_at - timedelta(seconds=2),
        value=value,
        raw_payload={"point_code": dev.point_code, "value": value, "review": True},
        device_status_at_receipt="normal",
        partition_key=partition_key,
        risk_level=dev.risk_level,
        status="confirmed",
        note="复盘模拟读数",
        scenario_id=key,
        received_at=received_at,
    )
    db.add(log)
    db.flush()
    return log


def _make_batch(db, key: str, partition_key: str, logs: list[ReceiveLog], policy_version: int,
                created_at: datetime, processed_at: datetime | None, status: str = "done") -> Batch:
    batch = Batch(
        partition_key=partition_key,
        policy_version=policy_version,
        source="normal",
        status=status,
        item_count=len(logs),
        scenario_id=key,
        created_at=created_at,
        processed_at=processed_at,
    )
    db.add(batch)
    db.flush()
    for lg in logs:
        db.add(BatchItem(batch_id=batch.id, receive_log_id=lg.id, scenario_id=key))
    return batch


def _event(db, lane, kind, ts, seq, *, partition=None, log_id=None, batch_id=None,
           policy_version=None, payload=None, note="", actor="replay"):
    ev = ledger.record_event(
        db, kind, ts, lane=lane, event_seq=seq, partition_key=partition,
        receive_log_id=log_id, batch_id=batch_id, policy_version=policy_version,
        payload=payload or {}, note=note, actor=actor,
    )
    if kind == "batch_create" and batch_id is not None:
        b = db.get(Batch, batch_id)
        if b is not None:
            b.create_event_seq = seq
    return ev


def _checkpoint(db, lane, ts, partitions: dict, degraded: bool, policy_version: int, seq: int, note: str):
    cp = QueueCheckpoint(
        lane=lane,
        checkpoint_seq=seq,
        effective_ts=ts,
        degraded=degraded,
        policy_version=policy_version,
        partitions_json=partitions,
        scheduler_cursor=seq,
        detail={"note": note},
    )
    db.add(cp)
    db.flush()
    return cp


def _partition_snapshot(backlog_map: dict, inflight_map: dict | None = None) -> dict:
    inflight_map = inflight_map or {}
    out = {}
    for p in ("P-HIGH-1", "P-HIGH-2", "P-MED-1", "P-LOW-1"):
        b = int(backlog_map.get(p, 0))
        f = int(inflight_map.get(p, 0))
        out[p] = {
            "risk_class": {"P-HIGH-1": "high", "P-HIGH-2": "high", "P-MED-1": "medium", "P-LOW-1": "low"}[p],
            "confirmed": b + f,
            "batched": f,
            "processed": 0,
            "backlog": b,
            "inflight": f,
            "confirmed_cursor": 0,
            "batched_cursor": 0,
            "processed_cursor": 0,
        }
    return out


def _candidate_list(weights: dict, backlog_map: dict) -> list[dict]:
    cls = {"P-HIGH-1": "high", "P-HIGH-2": "high", "P-MED-1": "medium", "P-LOW-1": "low"}
    rows = []
    for p, w in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])):
        rows.append(
            {
                "partition_key": p,
                "weight": w,
                "backlog": int(backlog_map.get(p, 0)),
                "risk_class": cls.get(p),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 序列一：容量耗尽
# ---------------------------------------------------------------------------

def _build_capacity(db, key: str, base: datetime) -> None:
    lane = _lane(key)
    t0 = base
    weights = {"P-HIGH-1": 100, "P-HIGH-2": 100, "P-MED-1": 50, "P-LOW-1": 10}
    backlog = {"P-HIGH-1": 6, "P-HIGH-2": 4, "P-MED-1": 0, "P-LOW-1": 0}

    _checkpoint(db, lane, t0, _partition_snapshot(backlog, {"P-HIGH-1": 2, "P-HIGH-2": 1}),
                degraded=False, policy_version=1, seq=1, note="区间起点：单通道已积压")

    _event(db, lane, "policy_effective", t0 + timedelta(seconds=1), 10, policy_version=2,
           payload={"version": 2, "name": "容量观察策略 v2", "priority": {"high": 100, "medium": 50, "low": 10},
                    "reserve_high_writes": 50, "weights": weights},
           note="场景基线策略（与 v1 权重一致，用于『沿用上一策略』比较）")

    # 目标：高风险读数，容量被已有高风险批次占满，等待后才有通道。
    target_log = _make_reading(db, key, "P-HIGH-1", t0 + timedelta(seconds=5), 150.0, session_tag="target")
    _event(db, lane, "receive", target_log.received_at, 20, partition="P-HIGH-1", log_id=target_log.id,
           payload={"device_point": target_log.device_point, "value": target_log.value},
           note="高风险目标读数到达，进入实时通道")

    # 已有占用通道的高风险批次（在途）
    pre_logs = [_make_reading(db, key, "P-HIGH-2", t0 + timedelta(seconds=8), 62.0, session_tag="pre2")]
    pre_b = _make_batch(db, key, "P-HIGH-2", pre_logs, 1, t0 + timedelta(seconds=20), t0 + timedelta(seconds=80))
    _event(db, lane, "batch_create", t0 + timedelta(seconds=20), 21, partition="P-HIGH-2", batch_id=pre_b.id,
           policy_version=1,
           payload={"candidates": _candidate_list(weights, backlog), "selected": "P-HIGH-2", "degraded": False,
                    "explain": "高风险分区 P-HIGH-2 积压 4，按 v1 权重 100 先占用通道", "cost_note": "通道单槽"},
           note="P-HIGH-2 先入批")
    _event(db, lane, "batch_complete", t0 + timedelta(seconds=80), 22, partition="P-HIGH-2", batch_id=pre_b.id,
           policy_version=1, payload={"item_count": pre_b.item_count}, note="P-HIGH-2 批次完成")

    pre1_logs = [_make_reading(db, key, "P-HIGH-1", t0 + timedelta(seconds=10), 140.0, session_tag="pre1")]
    pre1 = _make_batch(db, key, "P-HIGH-1", pre1_logs, 1, t0 + timedelta(seconds=35), t0 + timedelta(seconds=95))
    _event(db, lane, "batch_create", t0 + timedelta(seconds=35), 23, partition="P-HIGH-1", batch_id=pre1.id,
           policy_version=1,
           payload={"candidates": _candidate_list(weights, backlog), "selected": "P-HIGH-1", "degraded": False,
                    "explain": "P-HIGH-1 积压 6，优先级最高，但通道仍被 P-HIGH-2 占用，只能排队", "cost_note": "通道单槽"},
           note="P-HIGH-1 已有批次排队")

    # 目标批次直到容量释放后才生成
    tgt_created = t0 + timedelta(seconds=90)
    tgt = _make_batch(db, key, "P-HIGH-1", [target_log], 2, tgt_created, tgt_created + timedelta(seconds=6))
    _event(db, lane, "batch_create", tgt_created, 24, partition="P-HIGH-1", batch_id=tgt.id, policy_version=2,
           payload={"candidates": _candidate_list(weights, {"P-HIGH-1": 1, "P-HIGH-2": 0, "P-MED-1": 0, "P-LOW-1": 0}),
                    "selected": "P-HIGH-1", "degraded": False,
                    "explain": "目标读数已在 P-HIGH-1 积压 85s，容量释放后才被选中", "cost_note": "通道单槽"},
           note="容量释放后目标入批")
    _event(db, lane, "batch_complete", tgt_created + timedelta(seconds=6), 25, partition="P-HIGH-1", batch_id=tgt.id,
           policy_version=2, payload={"item_count": 1}, note="目标批次完成")

    concl = QualityConclusion(
        receive_log_id=target_log.id, batch_id=tgt.id, device_point=target_log.device_point,
        partition_key="P-HIGH-1", policy_version=2, range_ok=False, ts_ok=True, device_status_ok=True,
        gap_detected=False, conclusion="exceed", confirmed=True,
        detail="数值超出阈值（复盘模拟）", scenario_id=key,
    )
    db.add(concl)
    db.flush()
    db.commit()


# ---------------------------------------------------------------------------
# 序列二：低风险阻塞
# ---------------------------------------------------------------------------

def _build_low_risk(db, key: str, base: datetime) -> None:
    lane = _lane(key)
    t0 = base
    weights = {"P-HIGH-1": 100, "P-HIGH-2": 100, "P-MED-1": 50, "P-LOW-1": 10}
    backlog = {"P-HIGH-1": 5, "P-LOW-1": 0}

    _checkpoint(db, lane, t0, _partition_snapshot(backlog, {"P-HIGH-1": 1, "P-LOW-1": 1}),
                degraded=True, policy_version=1, seq=1, note="区间起点：已降级，低风险批次仍在途")
    _event(db, lane, "decision", t0 + timedelta(seconds=1), 5,
           payload={"type": "degraded_toggle", "degraded": True, "low_risk_paused": True},
           note="写入延迟跨阈值 → 调度器降级，但既有低风险批次已在通道内")

    _event(db, lane, "policy_effective", t0 + timedelta(seconds=2), 6, policy_version=2,
           payload={"version": 2, "name": "低风险阻塞观察策略 v2",
                    "priority": {"high": 100, "medium": 50, "low": 10}, "reserve_high_writes": 60, "weights": weights},
           note="场景基线策略（权重与 v1 一致）")

    # 低风险批次先占用单通道，处理耗时很长
    low_logs = [
        _make_reading(db, key, "P-LOW-1", t0 + timedelta(seconds=4 + i), 5.0 + i, prefer_high=False, session_tag=f"low{i}")
        for i in range(5)
    ]
    low_b = _make_batch(db, key, "P-LOW-1", low_logs, 1, t0 + timedelta(seconds=10), t0 + timedelta(seconds=100))
    _event(db, lane, "batch_create", t0 + timedelta(seconds=10), 7, partition="P-LOW-1", batch_id=low_b.id,
           policy_version=1,
           payload={"candidates": _candidate_list(weights, backlog), "selected": "P-LOW-1", "degraded": True,
                    "explain": "低风险批次在降级前已进入通道，降级只暂停后续衍生计算，未能让出已在途槽位",
                    "cost_note": "通道单槽被低风险占用 90s"},
           note="低风险批次占用通道")

    target_log = _make_reading(db, key, "P-HIGH-1", t0 + timedelta(seconds=15), 155.0, session_tag="target")
    _event(db, lane, "receive", target_log.received_at, 8, partition="P-HIGH-1", log_id=target_log.id,
           payload={"device_point": target_log.device_point, "value": target_log.value},
           note="高风险目标读数到达，但通道被低风险占用")

    tgt_created = t0 + timedelta(seconds=60)
    tgt = _make_batch(db, key, "P-HIGH-1", [target_log], 2, tgt_created, tgt_created + timedelta(seconds=8))
    _event(db, lane, "batch_create", tgt_created, 9, partition="P-HIGH-1", batch_id=tgt.id, policy_version=2,
           payload={"candidates": _candidate_list(weights, {"P-HIGH-1": 5, "P-LOW-1": 5, "P-HIGH-2": 0, "P-MED-1": 0}),
                    "selected": "P-HIGH-1", "degraded": True,
                    "explain": "目标高风险已等待 45s；低风险批次仍占用通道，目标只能排队",
                    "cost_note": "低风险占槽导致高风险延迟"},
           note="目标高风险入批（被低风险阻塞）")
    _event(db, lane, "batch_complete", tgt_created + timedelta(seconds=8), 10, partition="P-HIGH-1", batch_id=tgt.id,
           policy_version=2, payload={"item_count": 1}, note="目标批次完成")
    _event(db, lane, "batch_complete", t0 + timedelta(seconds=100), 11, partition="P-LOW-1", batch_id=low_b.id,
           policy_version=1, payload={"item_count": low_b.item_count}, note="低风险批次最终完成")

    db.add(QualityConclusion(
        receive_log_id=target_log.id, batch_id=tgt.id, device_point=target_log.device_point,
        partition_key="P-HIGH-1", policy_version=2, range_ok=False, ts_ok=True, device_status_ok=True,
        gap_detected=False, conclusion="exceed", confirmed=True,
        detail="数值超出阈值（复盘模拟）", scenario_id=key,
    ))
    db.flush()
    db.commit()


# ---------------------------------------------------------------------------
# 序列三：策略切换竞争（同刻按预登记序号裁定）
# ---------------------------------------------------------------------------

def _build_policy_race(db, key: str, base: datetime) -> None:
    lane = _lane(key)
    t0 = base
    T = t0 + timedelta(seconds=60)
    parts = _partition_snapshot({"P-HIGH-1": 4, "P-MED-1": 3}, {"P-HIGH-1": 1})
    _checkpoint(db, lane, t0, parts, degraded=False, policy_version=1, seq=1,
                note="区间起点：切换前水位")

    prev_weights = {"P-HIGH-1": 100, "P-HIGH-2": 100, "P-MED-1": 50, "P-LOW-1": 10}
    new_weights = {"P-HIGH-1": 60, "P-HIGH-2": 60, "P-MED-1": 50, "P-LOW-1": 45}

    target_log = _make_reading(db, key, "P-HIGH-1", t0 + timedelta(seconds=55), 158.0, session_tag="target")
    _event(db, lane, "receive", target_log.received_at, 30, partition="P-HIGH-1", log_id=target_log.id,
           payload={"device_point": target_log.device_point, "value": target_log.value},
           note="目标高风险读数在切换前到达")

    # 一个中风险批次恰好也在 T 完成（与切换同一生效时刻）
    med_logs = [_make_reading(db, key, "P-MED-1", t0 + timedelta(seconds=20 + i), 7.0, prefer_high=False,
                              session_tag=f"med{i}") for i in range(3)]
    med_b = _make_batch(db, key, "P-MED-1", med_logs, 1, t0 + timedelta(seconds=30), T)
    _event(db, lane, "batch_create", t0 + timedelta(seconds=30), 31, partition="P-MED-1", batch_id=med_b.id,
           policy_version=1,
           payload={"candidates": _candidate_list(prev_weights, {"P-HIGH-1": 4, "P-MED-1": 3}),
                    "selected": "P-MED-1", "degraded": False, "explain": "中风险批次先入批", "cost_note": "通道单槽"},
           note="中风险批次入批")

    # 关键：三个事件同一生效时刻 T。登记序号：policy(100) < complete(101) < target batch(102)，
    # 但数据库插入顺序刻意反转为 complete -> policy -> batch_create，用于验证顺序规范器。
    tgt = _make_batch(db, key, "P-HIGH-1", [target_log], 2, T, T + timedelta(seconds=10))
    tgt.create_event_seq = 102
    db.flush()

    # 先插入 batch_complete（数据库 id 最小）
    _event(db, lane, "batch_complete", T, 101, partition="P-MED-1", batch_id=med_b.id, policy_version=1,
           payload={"item_count": med_b.item_count}, note="中风险批次完成（与切换同刻，写入先于策略）")
    # 再插入 policy_effective
    _event(db, lane, "policy_effective", T, 100, policy_version=2,
           payload={"version": 2, "name": "均衡策略 v2", "priority": {"high": 60, "medium": 50, "low": 45},
                    "reserve_high_writes": 50, "weights": new_weights},
           note="策略 v2 于 T 生效（同刻登记序号 100 最小 → 先于批次完成裁定）")
    # 最后插入 batch_create
    _event(db, lane, "batch_create", T, 102, partition="P-HIGH-1", batch_id=tgt.id, policy_version=2,
           payload={"candidates": _candidate_list(new_weights, {"P-HIGH-1": 4, "P-MED-1": 0}),
                    "selected": "P-HIGH-1", "degraded": False,
                    "explain": "同刻竞争：按预登记序号先应用 v2（高风险权重降为 60）再完成中风险批次，"
                               "目标等待由切换后的权重决定", "cost_note": "切换与完成同刻"},
           note="目标批次在切换同刻生成")
    _event(db, lane, "batch_complete", T + timedelta(seconds=10), 103, partition="P-HIGH-1", batch_id=tgt.id,
           policy_version=2, payload={"item_count": 1}, note="目标批次完成")

    db.add(QualityConclusion(
        receive_log_id=target_log.id, batch_id=tgt.id, device_point=target_log.device_point,
        partition_key="P-HIGH-1", policy_version=2, range_ok=False, ts_ok=True, device_status_ok=True,
        gap_detected=False, conclusion="exceed", confirmed=True,
        detail="数值超出阈值（复盘模拟）", scenario_id=key,
    ))
    db.flush()
    db.commit()


BUILDERS = {}


# ---------------------------------------------------------------------------
# 序列四：事件链断裂保护（只允许“证据不足”，不臆测）
# ---------------------------------------------------------------------------

def _build_insufficient(db, key: str, base: datetime) -> None:
    lane = _lane(key)
    t0 = base
    # 目标读数已入批，但刻意缺失它的“接收”事件，且没有检查点 → 事件链断裂。
    target_log = _make_reading(db, key, "P-HIGH-1", t0 + timedelta(seconds=5), 151.0, session_tag="broken")
    pre = _make_reading(db, key, "P-HIGH-2", t0 + timedelta(seconds=2), 60.0, session_tag="broken2")
    b = _make_batch(db, key, "P-HIGH-1", [target_log], 1, t0 + timedelta(seconds=60), t0 + timedelta(seconds=70))
    _make_batch(db, key, "P-HIGH-2", [pre], 1, t0 + timedelta(seconds=10), t0 + timedelta(seconds=40))
    # 仅登记批次生成与完成，缺少目标 receive 事件、缺少起点检查点。
    _event(db, lane, "batch_create", t0 + timedelta(seconds=60), 5, partition="P-HIGH-1", batch_id=b.id,
           policy_version=1, payload={"selected": "P-HIGH-1", "candidates": [], "degraded": False},
           note="目标批次生成（接收事件缺失）")
    _event(db, lane, "batch_complete", t0 + timedelta(seconds=70), 6, partition="P-HIGH-1", batch_id=b.id,
           policy_version=1, payload={"item_count": 1}, note="目标批次完成")
    db.commit()


BUILDERS = {
    "capacity": (_build_capacity, "容量耗尽"),
    "low_risk": (_build_low_risk, "低风险阻塞"),
    "policy_race": (_build_policy_race, "策略切换竞争"),
    "insufficient": (_build_insufficient, "事件链断裂保护（证据不足）"),
}


# ---------------------------------------------------------------------------
# 构建 / 删除 / 复核
# ---------------------------------------------------------------------------

def _scenario_rows(db, key: str):
    return db.query(ScenarioRun).filter(ScenarioRun.scenario_key == key).all()


def build_scenario(db, key: str, force: bool = False) -> dict:
    if key not in BUILDERS:
        raise ValueError(f"未知场景 {key}")
    existing = _scenario_rows(db, key)
    if existing and not force:
        return review_svc.scenario_detail(db, key)
    if existing:
        _purge_lane(db, key)

    builder, label = BUILDERS[key]
    base = datetime.utcnow().replace(microsecond=0) - timedelta(minutes=15)
    builder(db, key, base)
    db.commit()

    # 区间窗口：覆盖序列全部事件
    evs = (
        db.query(DecisionEvent)
        .filter(DecisionEvent.lane == _lane(key))
        .order_by(DecisionEvent.effective_ts)
        .all()
    )
    start = evs[0].effective_ts - timedelta(seconds=1)
    end = evs[-1].effective_ts + timedelta(seconds=5)
    target = db.query(ReceiveLog).filter(ReceiveLog.scenario_id == key).order_by(ReceiveLog.id).first()
    # 取“目标读数”：优先取第一次 reading 事件指向的日志（这里即目标）
    target_event = next((e for e in evs if e.kind == "receive"), None)
    target_log_id = target_event.receive_log_id if target_event else (target.id if target else None)

    ordering = _ordering_check(db, _lane(key), getattr(evs[0], "effective_ts", None))
    db.add(ScenarioRun(
        scenario_key=key, label=label, lane=_lane(key), version=1, status="ready",
        note="诊断性模拟序列", detail={"window": [start.isoformat(), end.isoformat()], "ordering_check": ordering},
    ))
    db.commit()

    review_svc.issue_review(
        db, _lane(key), "scenario", key, start, end, target_log_id=target_log_id,
        issued_by="replay-service",
    )
    return review_svc.scenario_detail(db, key)


def _ordering_check(db, lane: str, at: datetime | None) -> dict:
    """对比“数据库写入先后”与“预登记序号裁定”两种顺序，证明规范器不依赖写入顺序。"""
    if at is None:
        return {}
    same_ts = (
        db.query(DecisionEvent)
        .filter(DecisionEvent.lane == lane, DecisionEvent.effective_ts == at)
        .all()
    )
    if len(same_ts) < 2:
        # 找到任一有 >=2 事件同刻的时刻
        from collections import defaultdict

        groups = defaultdict(list)
        for e in db.query(DecisionEvent).filter(DecisionEvent.lane == lane).all():
            groups[e.effective_ts].append(e)
        same_ts = max(groups.values(), key=len) if groups else []
    by_db = sorted(same_ts, key=lambda e: e.id)
    by_seq = ledger.normalize_order(same_ts)
    return {
        "effective_ts": same_ts[0].effective_ts.isoformat() if same_ts else None,
        "by_db_write_order": [(e.id, e.event_seq, e.kind) for e in by_db],
        "by_registered_seq": [(e.id, e.event_seq, e.kind) for e in by_seq],
        "differs": [e.id for e in by_db] != [e.id for e in by_seq],
        "ruling": "同一生效时刻按预登记 event_seq 裁定，不依赖数据库写入先后",
    }


def _purge_lane(db, key: str) -> None:
    """仅清理模拟通道（scenario:*），不触碰生产台账。"""
    lane = _lane(key)
    db.query(BatchItem).filter(BatchItem.scenario_id == key).delete()
    db.query(QualityConclusion).filter(QualityConclusion.scenario_id == key).delete()
    db.query(Batch).filter(Batch.scenario_id == key).delete()
    db.query(ReceiveLog).filter(ReceiveLog.scenario_id == key).delete()
    db.query(DecisionEvent).filter(DecisionEvent.lane == lane).delete()
    db.query(QueueCheckpoint).filter(QueueCheckpoint.lane == lane).delete()
    db.query(ReviewRun).filter(ReviewRun.lane == lane).delete()
    db.query(ScenarioRun).filter(ScenarioRun.scenario_key == key).delete()
    db.commit()


def rebuild_scenario(db, key: str) -> dict:
    return build_scenario(db, key, force=True)


def apply_late_event(db, key: str) -> dict:
    """补传晚到：向已复盘区间追加晚到数据，生成新的复盘版本与差异说明（不改写旧结论）。

    演示为一次低风险批次补传落入区间中部——它会改变“若低风险任务未占用通道”的比较。
    """
    if key not in BUILDERS:
        raise ValueError(f"未知场景 {key}")
    run = db.query(ScenarioRun).filter(ScenarioRun.scenario_key == key).order_by(ScenarioRun.id.desc()).first()
    if run is None:
        raise ValueError("场景尚未构建")
    lane = _lane(key)
    detail = run.detail or {}
    win_start = datetime.fromisoformat(detail["window"][0])
    win_end = datetime.fromisoformat(detail["window"][1])
    mid = win_start + (win_end - win_start) / 2

    # 晚到数据：低风险分区补传 30 条，在区间中部创建批次（effective_ts 落在已复盘区间内）
    seq_base = (db.query(func.max(DecisionEvent.event_seq)).filter(DecisionEvent.lane == lane).scalar() or 0) + 1000
    late_logs = [
        _make_reading(db, key, "P-LOW-1", mid - timedelta(seconds=5 + i), 4.0, prefer_high=False,
                      session_tag=f"late{i}")
        for i in range(30)
    ]
    late_b = _make_batch(db, key, "P-LOW-1", late_logs, 2, mid, mid + timedelta(seconds=40))
    _event(db, lane, "late_arrival", mid, seq_base, partition="P-LOW-1",
           payload={"reason": "补传晚到", "late_count": 30, "landed_in_reviewed_window": True},
           note="补传读数晚到，落入已复盘区间")
    for lg in late_logs[:1]:
        _event(db, lane, "receive", lg.received_at, seq_base + 1, partition="P-LOW-1", log_id=lg.id,
               payload={"device_point": lg.device_point, "value": lg.value, "late": True},
               note="补传接收（晚于原复盘签发）")
    _event(db, lane, "batch_create", mid, seq_base + 2, partition="P-LOW-1", batch_id=late_b.id, policy_version=2,
           payload={"candidates": _candidate_list({"P-HIGH-1": 100, "P-HIGH-2": 100, "P-MED-1": 50, "P-LOW-1": 10},
                                                  {"P-LOW-1": 30}),
                    "selected": "P-LOW-1", "degraded": False,
                    "explain": "补传低风险批次在区间中部占用通道", "cost_note": "补传晚到"},
           note="补传批次生成")
    _event(db, lane, "batch_complete", mid + timedelta(seconds=40), seq_base + 3, partition="P-LOW-1",
           batch_id=late_b.id, policy_version=2, payload={"item_count": 30}, note="补传批次完成")
    db.add(ScenarioRun(
        scenario_key=key, label=run.label, lane=lane, version=(run.version or 1) + 1, status="late_appended",
        note="补传晚到落入已复盘区间，追加新复盘版本",
        detail={**detail, "late_event_seq": seq_base, "late_at": mid.isoformat()},
    ))
    db.commit()

    target = (
        db.query(DecisionEvent.receive_log_id)
        .filter(DecisionEvent.lane == lane, DecisionEvent.kind == "receive")
        .order_by(DecisionEvent.event_seq)
        .first()
    )
    target_log_id = target[0] if target else None
    review_svc.issue_review(
        db, lane, "scenario", key, win_start, win_end, target_log_id=target_log_id,
        issued_by="replay-service", allow_supersede=True,
    )
    return review_svc.scenario_detail(db, key)

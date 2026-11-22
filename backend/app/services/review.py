"""复盘服务：重建决策时间线、计算反事实差异、签发/版本化复盘结论。

核心约束（与需求一一对应）：

* **只读生产台账**：复盘只读取接收日志/批次/策略/事件序列，反事实在内存中计算，
  不修改任何生产台账行。
* **同刻按预登记序号裁定**：顺序规范器按 ``event_seq`` 裁决同一生效时刻的策略切换
  与批次完成先后，绝不依赖数据库写入先后。
* **证据不足不臆测**：反事实缺少当时水位、或事件链断裂时，比较器返回
  ``insufficient`` 并用 *当前* 队列状态佐证“这不是历史答案”，而不用当前状态冒充历史。
* **补传晚到 → 新版本**：晚到事件落入已签发区间时，冻结旧结论、生成新版本与差异说明。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import config
from ..models import (
    Alert,
    Batch,
    BatchItem,
    DecisionEvent,
    Partition,
    Policy,
    QualityConclusion,
    ReceiveLog,
    ReviewRun,
    ScenarioRun,
)
from . import decision_ledger as ledger


class InsufficientEvidence(Exception):
    def __init__(self, reason: str, current_state: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.current_state = current_state or {}


# ---------------------------------------------------------------------------
# 台账数据访问（按通道隔离）
# ---------------------------------------------------------------------------

def _lane_scope(lane: str) -> dict:
    return {"scenario_id": None} if lane == ledger.LIVE else {"scenario_id": ledger.scenario_of_lane(lane)}


def _f(model, lane: str):
    if lane == ledger.LIVE:
        # live 口径只含场景列为空的真实台账行（避免把模拟数据混进生产复盘）
        return [model.scenario_id.is_(None)]
    return [getattr(model, k) == v for k, v in _lane_scope(lane).items()]


def lane_events(db, lane: str, start=None, end=None) -> list[DecisionEvent]:
    q = db.query(DecisionEvent).filter(DecisionEvent.lane == lane)
    if start is not None:
        q = q.filter(DecisionEvent.effective_ts >= start)
    if end is not None:
        q = q.filter(DecisionEvent.effective_ts <= end)
    return ledger.normalize_order(q.all())


def lane_policies(db, lane: str) -> list[dict]:
    """通道内可见的策略版本，并附上生效序号（决定同刻先后）。

    * ``live``：读取策略台账（Policy 表）+ 生效事件序号。
    * ``scenario:*``：只取种子基础版本 v1，叠加该通道内 ``policy_effective`` 事件
      携带的策略快照。场景策略不写入生产策略台账，避免污染监管台账。
    """
    seqs = ledger.effective_seq_map(db, lane)
    rows = []
    base_q = db.query(Policy).order_by(Policy.version)
    if ledger.is_scenario_lane(lane):
        base_q = base_q.filter(Policy.version == 1)
    for p in base_q.all():
        rows.append(
            {
                "version": p.version,
                "name": p.name,
                "priority": p.priority_json,
                "reserve_high_writes": p.reserve_high_writes,
                "effective_ts": p.effective_ts,
                "event_seq": seqs.get(p.version),
                "active": p.active,
            }
        )
    if ledger.is_scenario_lane(lane):
        for e in (
            db.query(DecisionEvent)
            .filter(DecisionEvent.lane == lane, DecisionEvent.kind == "policy_effective")
            .order_by(DecisionEvent.event_seq)
            .all()
        ):
            payload = e.payload or {}
            rows.append(
                {
                    "version": payload.get("version", e.policy_version),
                    "name": payload.get("name", f"策略 v{e.policy_version}"),
                    "priority": payload.get("priority") or {},
                    "reserve_high_writes": payload.get("reserve_high_writes", 50),
                    "effective_ts": e.effective_ts,
                    "event_seq": e.event_seq,
                    "active": True,
                }
            )
    rows.sort(key=lambda r: r["version"])
    return rows


def policy_at(policies: list[dict], ts: datetime, seq: int | None, lane_has_events: bool,
              at_seq: int | None = None) -> dict | None:
    """返回在 (ts, seq) 时刻已生效的最后一版策略。

    * 无 ``seq`` 时按 (生效时刻, 登记序号) 取不晚于 ts 的最新版本；
    * 给出 ``at_seq``（如目标批次的事件序号）时，同刻策略只有登记序号更小时才算已生效，
      这正是“同一生效时刻按预登记序号裁定”的落点。
    若该通道没有自己的策略生效事件，则回退到版本号最小的基础版本（种子 v1）。
    """
    eligible = []
    for p in policies:
        eff = p.get("effective_ts")
        pseq = p.get("event_seq")
        if eff is None and pseq is None:
            # 基础版本（种子 v1）：始终视为在最早时刻生效。
            eligible.append((datetime.min, -1, p))
            continue
        eff = eff or datetime.min
        if eff > ts:
            continue
        if eff == ts:
            if seq is not None and pseq is not None and pseq > seq:
                continue
            if at_seq is not None and pseq is not None and pseq > at_seq:
                continue
        eligible.append((eff, pseq if pseq is not None else 10**12, p))
    if not eligible:
        return None
    eligible.sort(key=lambda x: (x[0], x[1]))
    return eligible[-1][2]


def effective_weights(policy: dict, partitions: dict[str, str]) -> dict[str, int]:
    """把一个策略版本的按风险级权重展开为按分区的权重。"""
    if not policy:
        return {}
    weights = policy.get("priority") or {}
    out = {}
    for pkey, cls in partitions.items():
        out[pkey] = int(weights.get(cls, 0) or 0)
    return out


# ---------------------------------------------------------------------------
# 时间线重建
# ---------------------------------------------------------------------------

def _iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt


def _event_brief(e: DecisionEvent) -> dict:
    p = e.payload or {}
    return {
        "event_seq": e.event_seq,
        "kind": e.kind,
        "effective_ts": _iso(e.effective_ts),
        "recorded_at": _iso(e.recorded_at),
        "actor": e.actor,
        "partition_key": e.partition_key,
        "receive_log_id": e.receive_log_id,
        "batch_id": e.batch_id,
        "policy_version": e.policy_version,
        "note": e.note,
        "detail": p,
    }


def _partition_classes(db) -> dict[str, str]:
    return {p.partition_key: p.risk_class for p in db.query(Partition).all()}


def build_timeline(db, lane: str, start: datetime, end: datetime, target_log: ReceiveLog | None) -> dict:
    """从检查点锚点 + 事件序列重建决策时间线（逐步“可选批次”与“实际选择”）。"""
    policies = lane_policies(db, lane)
    parts_cls = _partition_classes(db)
    events = lane_events(db, lane, start, end)

    chain_events = [e for e in events if e.kind in ("receive", "batch_create", "policy_effective", "batch_complete")]

    # 事件链完整性：目标读数及其批次相关事件是否齐全。
    chain_complete = True
    gaps = []
    if target_log is not None:
        has_receive = any(e.kind == "receive" and e.receive_log_id == target_log.id for e in chain_events)
        if not has_receive:
            chain_complete = False
            gaps.append(f"缺少接收日志 #{target_log.id} 的接收事件，事件链断裂")
        item = db.query(BatchItem).filter(BatchItem.receive_log_id == target_log.id).first()
        if item is not None:
            has_batch = any(e.kind == "batch_create" and e.batch_id == item.batch_id for e in chain_events)
            if not has_batch:
                chain_complete = False
                gaps.append(f"目标批次 #{item.batch_id} 的生成事件缺失，事件链断裂")

    # 每步：同刻可见的候选分区 + 在当前策略/降级态下实际选择
    steps = []
    policy_versions_by_ts: list[tuple[datetime, int, dict]] = []
    for p in policies:
        eff = p.get("effective_ts")
        pseq = p.get("event_seq")
        if eff is None and pseq is None:
            policy_versions_by_ts.append((datetime.min, -1, p))
        else:
            policy_versions_by_ts.append((eff or datetime.min, pseq if pseq is not None else 10**12, p))
    policy_versions_by_ts.sort(key=lambda x: (x[0], x[1]))

    degraded_events = [e for e in events if e.kind == "decision" and (e.payload or {}).get("type") == "degraded_toggle"]

    # 按批次生成事件逐步推演；“可选”为该刻已确认、未入批、且在实时窗口内的分区批次，
    # “实际选择”取自事件台账里登记的选定分区。
    batch_create_events = [e for e in events if e.kind == "batch_create"]
    for e in batch_create_events:
        pol = policy_at(
            policies, e.effective_ts, e.event_seq, bool(policy_versions_by_ts)
        )
        weights = effective_weights(pol, parts_cls)
        p = e.payload or {}
        candidates = p.get("candidates") or []
        selected = p.get("selected")
        steps.append(
            {
                "event_seq": e.event_seq,
                "at": e.effective_ts,
                "kind": "batch_create",
                "partition_key": e.partition_key,
                "batch_id": e.batch_id,
                "policy_version": pol["version"] if pol else None,
                "scoring": {
                    "weights": weights,
                    "reserve_high_writes": (pol or {}).get("reserve_high_writes"),
                    "degraded": p.get("degraded", False),
                },
                "candidates": candidates,
                "selected": selected or e.partition_key,
                "explain": p.get("explain", ""),
                "cost_note": p.get("cost_note", ""),
            }
        )

    checkpoint = ledger.latest_checkpoint(db, lane, at_or_before=start)
    precision = ledger.mode_info()["precision"]
    if checkpoint is None:
        notes = ["无区间起点之前的检查点，时间线自区间内首个事件重建"]
    else:
        notes = [f"以检查点 #{checkpoint.id}（seq≤{checkpoint.checkpoint_seq}）为起点锚定"]

    return {
        "events": [_event_brief(e) for e in events],
        "steps": steps,
        "chain_complete": chain_complete,
        "chain_gaps": gaps,
        "anchor": {
            "checkpoint_id": checkpoint.id if checkpoint else None,
            "checkpoint_seq": checkpoint.checkpoint_seq if checkpoint else None,
            "partitions": checkpoint.partitions_json if checkpoint else None,
            "scheduler_cursor": checkpoint.scheduler_cursor if checkpoint else None,
        },
        "precision": precision,
        "notes": notes,
        "policy_versions": [
            {
                "version": p["version"],
                "name": p["name"],
                "effective_ts": _iso(p["effective_ts"]),
                "event_seq": p["event_seq"],
                "active": p["active"],
            }
            for p in policies
        ],
        "degraded_changes": [_event_brief(e) for e in degraded_events],
    }


# ---------------------------------------------------------------------------
# 反事实比较
# ---------------------------------------------------------------------------

def _current_queue_state(db, lane: str) -> dict:
    return ledger.snapshot_partitions(db, lane)


def _target_observation(db, lane: str, target_log: ReceiveLog | None, target_batch: Batch | None) -> dict:
    if target_log is None:
        raise InsufficientEvidence("指定告警/读数不存在，无法重建等待来源")
    obs = {
        "receive_log_id": target_log.id,
        "partition_key": target_log.partition_key,
        "received_at": target_log.received_at.isoformat(),
        "reading_ts": target_log.reading_ts.isoformat(),
        "batch_id": target_batch.id if target_batch else None,
        "batch_created_at": target_batch.created_at.isoformat() if target_batch and target_batch.created_at else None,
        "batch_processed_at": target_batch.processed_at.isoformat() if target_batch and target_batch.processed_at else None,
        "policy_version": target_batch.policy_version if target_batch else None,
    }
    if target_batch and target_batch.created_at:
        obs["wait_ms"] = int((target_batch.created_at - target_log.received_at).total_seconds() * 1000)
    if target_batch and target_batch.created_at and target_batch.processed_at:
        obs["total_ms"] = int((target_batch.processed_at - target_log.received_at).total_seconds() * 1000)
    return obs


def compare_counterfactual(db, lane: str, diagnosis: dict, target_log: ReceiveLog | None) -> dict:
    """计算“若低风险任务未占用通道”与“若沿用上一策略”的等待差异。

    缺少当时队列水位 / 调度游标 / 事件链时返回 ``insufficient``。
    """
    parts_cls = _partition_classes(db)
    policies = lane_policies(db, lane)
    start = diagnosis["window_start"]
    end = diagnosis["window_end"]
    events = lane_events(db, lane, start, end)
    chain = [e for e in events if e.kind in ("receive", "batch_create", "policy_effective", "batch_complete")]

    item = None
    target_batch = None
    if target_log is not None:
        item = db.query(BatchItem).filter(BatchItem.receive_log_id == target_log.id).first()
        if item is not None:
            target_batch = db.get(Batch, item.batch_id)

    obs = _target_observation(db, lane, target_log, target_batch)
    current = _current_queue_state(db, lane)

    insufficient = []
    if target_log is None:
        insufficient.append("缺少目标读数")
    if target_batch is None:
        insufficient.append("目标读数尚未入批，无法比较等待差异")
    if not any(e.kind == "batch_create" for e in chain):
        insufficient.append("区间内无批次生成事件，事件链断裂")
    # 关键：反事实需要“当时”的队列水位。检查点缺失或早于区间起点太远即判证据不足。
    cp = ledger.latest_checkpoint(db, lane, at_or_before=start)
    anchor_parts = (cp.partitions_json if cp else None)
    if not anchor_parts:
        insufficient.append("缺少区间起点的队列水位检查点（调度游标/积压不可回溯）")

    if insufficient:
        return {
            "status": "insufficient",
            "reasons": insufficient,
            "evidence_note": "不以当前队列状态冒充历史答案；下列“当前状态”仅供说明并非当时答案",
            "current_queue_state_not_historical": current,
            "comparisons": [],
        }

    # 重建当时（目标批次生成时刻）其它批次的可比等待。
    at = target_batch.created_at or target_log.received_at
    target_wait = obs.get("wait_ms", 0)
    target_part = target_log.partition_key

    # 1) 若低风险任务未占用通道：去掉低风险分区批次的竞争后，目标等待的缩短量。
    competitors = (
        db.query(Batch)
        .filter(Batch.created_at <= at, Batch.created_at >= start, *(_f(Batch, lane)))
        .all()
    )
    low_risk_competitors = [
        b for b in competitors
        if b.partition_key != target_part and parts_cls.get(b.partition_key) == "low" and b.item_count
    ]
    low_risk_items = sum(b.item_count for b in low_risk_competitors)
    # 单通道吞吐近似：低风险占用条目推迟了目标批次的生成，缩短量 = 低风险条目 / 吞吐。
    # 吞吐用目标批次前一个处理周期内已处理批次条目量估计。
    throughput = max(
        sum(b.item_count for b in competitors if b.processed_at) or target_batch.item_count or 1,
        1,
    )
    saved_low_risk_ms = int(min(target_wait, low_risk_items / max(throughput, 1) * max(target_wait, 1)))

    # 2) 若沿用上一策略：按上一版本权重重排当时可选批次，估算目标被提前的等待。
    #    目标批次同刻生效的策略是否“已生效”，由目标批次预登记序号裁定。
    applied_pol = policy_at(policies, at, None, True, at_seq=target_batch.create_event_seq)
    prev = None
    if applied_pol is not None:
        earlier = [p for p in policies if p["version"] < applied_pol["version"]]
        if earlier:
            prev = max(earlier, key=lambda p: p["version"])
    if prev is not None and applied_pol is not None:
        w_new = effective_weights(applied_pol, parts_cls)
        w_old = effective_weights(prev, parts_cls)
        target_w_new = w_new.get(target_part, 0)
        target_w_old = w_old.get(target_part, 0)
        # 目标分区权重被新策略下调 → 沿用旧策略会更早被选中（等待缩短）；
        # 权重被上调 → 沿用旧策略反而更晚（等待变长）。差异按权重比例折算。
        if target_w_new > 0:
            ratio = (target_w_new - target_w_old) / float(target_w_new)
        else:
            ratio = 0.0
        # wait_delta = 反事实等待 - 实际等待；负值表示沿用旧策略更快。
        wait_delta = int(target_wait * ratio)
        policy_cmp = {
            "name": "沿用上一策略",
            "from_version": applied_pol["version"],
            "to_version": prev["version"],
            "weights_now": w_new,
            "weights_prev": w_old,
            "target_weight_now": target_w_new,
            "target_weight_prev": target_w_old,
            "actual_wait_ms": target_wait,
            "counterfactual_wait_ms": max(target_wait + wait_delta, 0),
            "wait_delta_ms": wait_delta,
            "explain": (
                f"沿用 v{prev['version']}（目标分区权重 {target_w_old}）而非 v{applied_pol['version']}"
                f"（权重 {target_w_new}），目标等待约{'缩短' if wait_delta < 0 else '延长' if wait_delta > 0 else '持平'} "
                f"{abs(wait_delta)}ms"
            ),
        }
    else:
        policy_cmp = {
            "name": "沿用上一策略",
            "status": "insufficient",
            "explain": "策略台账中无可比对的上一版本（或目标批次前尚无更早生效版本）",
        }

    comparisons = [
        {
            "name": "若低风险任务未占用通道",
            "low_risk_competitors": [
                {"batch_id": b.id, "partition_key": b.partition_key, "item_count": b.item_count}
                for b in low_risk_competitors
            ],
            "low_risk_items": low_risk_items,
            "actual_wait_ms": target_wait,
            "counterfactual_wait_ms": max(target_wait - saved_low_risk_ms, 0),
            "wait_delta_ms": -saved_low_risk_ms,
            "explain": (
                f"目标等待 {target_wait}ms；低风险批次占用 {low_risk_items} 条，"
                f"若让出通道目标等待约缩短 {saved_low_risk_ms}ms"
                if low_risk_items
                else "区间内无低风险批次与目标竞争，通道未被低风险占用"
            ),
        },
        policy_cmp,
    ]

    return {
        "status": "ok",
        "anchor_checkpoint_id": cp.id,
        "anchor_partitions": anchor_parts,
        "actual": obs,
        "comparisons": comparisons,
        "evidence_note": "反事实基于区间起始检查点的当时水位与事件序列重建，未读取/修改当前生产台账",
    }


# ---------------------------------------------------------------------------
# 诊断与结论
# ---------------------------------------------------------------------------

def diagnose(db, lane: str, target_log: ReceiveLog | None, start: datetime, end: datetime) -> dict:
    """按事件与当时水位判定等待来源：容量耗尽 / 低风险阻塞 / 策略切换 / 证据不足。"""
    parts_cls = _partition_classes(db)
    events = lane_events(db, lane, start, end)
    degraded_toggles = [
        e for e in events if e.kind == "decision" and (e.payload or {}).get("type") == "degraded_toggle"
    ]
    any_degraded = any((e.payload or {}).get("degraded") for e in degraded_toggles)

    if target_log is None:
        return {
            "verdict": "insufficient",
            "summary": "缺少目标告警/读数，无法定位等待来源",
            "window_start": start,
            "window_end": end,
            "signals": {},
        }
    item = db.query(BatchItem).filter(BatchItem.receive_log_id == target_log.id).first()
    if item is None:
        return {
            "verdict": "insufficient",
            "summary": f"读数 #{target_log.id} 尚未入批，事件链不足以定位等待来源",
            "window_start": start,
            "window_end": end,
            "signals": {"chain": "broken"},
        }
    has_receive = any(e.kind == "receive" and e.receive_log_id == target_log.id for e in events)
    if not has_receive:
        return {
            "verdict": "insufficient",
            "summary": f"事件链断裂：缺少读数 #{target_log.id} 的接收事件，无法重建当时顺序",
            "window_start": start,
            "window_end": end,
            "signals": {"chain": "broken", "missing_receive_event": True},
        }
    cp0 = ledger.latest_checkpoint(db, lane, at_or_before=start)
    if cp0 is None:
        return {
            "verdict": "insufficient",
            "summary": "缺少区间起点的队列水位检查点，无法还原当时积压与调度游标",
            "window_start": start,
            "window_end": end,
            "signals": {"checkpoint_missing": True},
        }
    target_batch = db.get(Batch, item.batch_id)
    target_part = target_log.partition_key
    target_cls = parts_cls.get(target_part)

    cp = ledger.latest_checkpoint(db, lane, at_or_before=start)
    anchor = (cp.partitions_json if cp else None) or {}
    part_anchor = anchor.get(target_part, {})

    # 同刻顺序竞争：目标批次生成前后是否有策略生效事件与批次完成事件同刻。
    same_ts_policy = [
        e for e in events
        if e.kind == "policy_effective" and target_batch is not None and e.effective_ts == target_batch.created_at
    ]
    same_ts_complete = [
        e for e in events
        if e.kind == "batch_complete" and target_batch is not None and e.effective_ts == target_batch.created_at
    ]

    # 当时（目标批次生成时刻）占用单通道、与目标等待区间重叠的批次
    # （含在目标到达之后、目标入批之前才创建的批次）。
    occupants = []
    low_risk_occupants = []
    if target_batch is not None and target_batch.created_at is not None:
        at = target_batch.created_at
        wait_start = target_log.received_at
        pol = policy_at(lane_policies(db, lane), at, None, True, at_seq=target_batch.create_event_seq)
        w = effective_weights(pol, parts_cls) if pol else {}
        for b in (
            db.query(Batch)
            .filter(Batch.created_at <= at, Batch.id != target_batch.id, *(_f(Batch, lane)))
            .all()
        ):
            ends = b.processed_at or (at + timedelta(seconds=1))
            # 区间 [b.created_at, b.processed_at] 与目标等待 [wait_start, at] 是否重叠
            if ends >= wait_start and b.created_at <= at:
                occupants.append(
                    {
                        "batch_id": b.id,
                        "partition_key": b.partition_key,
                        "risk_class": parts_cls.get(b.partition_key),
                        "item_count": b.item_count,
                        "weight": w.get(b.partition_key, 0),
                        "created_at": b.created_at.isoformat() if b.created_at else None,
                        "processed_at": b.processed_at.isoformat() if b.processed_at else None,
                    }
                )
        low_risk_occupants = [o for o in occupants if o["risk_class"] == "low"]

    signals = {
        "degraded_in_window": any_degraded,
        "anchor_backlog": part_anchor.get("backlog"),
        "anchor_inflight": part_anchor.get("inflight"),
        "same_ts_policy_events": len(same_ts_policy),
        "same_ts_complete_events": len(same_ts_complete),
        "target_risk_class": target_cls,
        "target_batch_id": target_batch.id if target_batch else None,
        "checkpoint_id": cp.id if cp else None,
        "channel_occupants": occupants,
        "low_risk_occupant_count": len(low_risk_occupants),
        "observed_wait_ms": (
            int((target_batch.created_at - target_log.received_at).total_seconds() * 1000)
            if target_batch and target_batch.created_at and target_log.received_at
            else None
        ),
    }
    observed_wait = signals["observed_wait_ms"]

    verdict = "unknown"
    summary = "无法从事件序列确定单一主因"
    if same_ts_policy and same_ts_complete:
        verdict = "policy_switch_race"
        summary = (
            "策略切换与批次完成拥有同一生效时刻，顺序由预登记事件序号裁定；"
            "目标等待源于切换时刻的调度竞争"
        )
    elif any_degraded and low_risk_occupants:
        verdict = "low_risk_blocked"
        summary = (
            f"降级期间 {len(low_risk_occupants)} 个低风险批次占用通道且与目标等待重叠，"
            "高风险目标读数被推迟，低风险任务未及时让位"
        )
    elif observed_wait is not None and observed_wait < 2000 and not occupants:
        verdict = "no_significant_wait"
        summary = f"目标读数接收→入批仅 {observed_wait}ms，单通道未发生显著排队，写入等待不构成延迟"
    elif occupants or (part_anchor.get("backlog") or 0) >= 1 or (part_anchor.get("inflight") or 0) >= 1:
        verdict = "capacity_exhausted"
        summary = (
            f"当时目标分区积压 {part_anchor.get('backlog')}、在途 {part_anchor.get('inflight')}，"
            f"另有 {len(occupants)} 个批次竞争同一单通道，容量耗尽导致写入等待"
        )
    elif same_ts_policy:
        verdict = "policy_switch_race"
        summary = "批次生成与策略生效同刻，顺序由预登记事件序号裁定"

    return {
        "verdict": verdict,
        "summary": summary,
        "window_start": start,
        "window_end": end,
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# 复盘签发（版本化）
# ---------------------------------------------------------------------------

def _latest_review(db, lane: str, scope_kind: str, scope_key: str) -> ReviewRun | None:
    return (
        db.query(ReviewRun)
        .filter(
            ReviewRun.lane == lane,
            ReviewRun.scope_kind == scope_kind,
            ReviewRun.scope_key == scope_key,
        )
        .order_by(ReviewRun.version.desc())
        .first()
    )


def _diff(old: dict, new: dict) -> dict:
    """生成两个复盘版本之间的差异说明。"""
    changes = []
    if old.get("verdict") != new.get("verdict"):
        changes.append(f"结论由 {old.get('verdict')} 变为 {new.get('verdict')}")
    old_cf = {c.get("name"): c for c in (old.get("counterfactual") or {}).get("comparisons", [])}
    new_cf = {c.get("name"): c for c in (new.get("counterfactual") or {}).get("comparisons", [])}
    for name, nc in new_cf.items():
        oc = old_cf.get(name)
        if oc is None:
            changes.append(f"新增反事实比较：{name}")
        elif oc.get("wait_delta_ms") != nc.get("wait_delta_ms"):
            changes.append(
                f"反事实「{name}」等待差异由 {oc.get('wait_delta_ms')}ms 变为 {nc.get('wait_delta_ms')}ms"
            )
    old_tl = old.get("timeline", {}).get("events", [])
    new_tl = new.get("timeline", {}).get("events", [])
    added = len(new_tl) - len(old_tl)
    if added:
        changes.append(f"时间线新增 {added} 个事件（含补传晚到事件）")
    return {
        "changes": changes or ["内容无变化"],
        "old_version": old.get("version"),
        "new_version": new.get("version"),
        "previous_content_hash": old.get("content_hash"),
    }


def issue_review(
    db,
    lane: str,
    scope_kind: str,
    scope_key: str,
    start: datetime,
    end: datetime,
    *,
    target_log_id: int | None = None,
    issued_by: str = "system",
    allow_supersede: bool = True,
) -> ReviewRun:
    """构建并签发复盘结论；若已有旧版本则生成新版本，绝不改写旧行。"""
    target_log = db.get(ReceiveLog, target_log_id) if target_log_id else None
    item = db.query(BatchItem).filter(BatchItem.receive_log_id == target_log.id).first() if target_log else None
    target_batch = db.get(Batch, item.batch_id) if item else None

    diagnosis = diagnose(db, lane, target_log, start, end)
    timeline = build_timeline(db, lane, start, end, target_log)

    if diagnosis["verdict"] == "insufficient" or not timeline["chain_complete"]:
        counter = {
            "status": "insufficient",
            "reasons": timeline["chain_gaps"] or [diagnosis.get("summary", "证据不足")],
            "current_queue_state_not_historical": _current_queue_state(db, lane),
            "comparisons": [],
            "evidence_note": "事件链断裂/缺少当时水位，比较器只给出证据不足；"
                             "所列当前状态仅供说明，并非当时答案",
        }
    else:
        counter = compare_counterfactual(db, lane, diagnosis, target_log)

    mode = ledger.mode_info()
    cost = ledger.cost_profile()
    precision = "insufficient" if counter.get("status") == "insufficient" else timeline["precision"]
    cost_note = (
        f"{mode['label']}；重建精度 {precision}；"
        f"检查点间隔 {config.REVIEW_CHECKPOINT_INTERVAL_SEC}s；"
        f"长区间重放成本约 {cost['checkpoint']['replay_cost_sec_per_hour_window']}s/小时区间"
    )

    result = {
        "lane": lane,
        "verdict": diagnosis["verdict"],
        "scope": {"kind": scope_kind, "key": scope_key},
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "diagnosis": {
            "verdict": diagnosis["verdict"],
            "summary": diagnosis["summary"],
            "signals": diagnosis["signals"],
        },
        "target": (
            {
                "receive_log_id": target_log.id,
                "device_point": target_log.device_point,
                "partition_key": target_log.partition_key,
                "value": target_log.value,
                "received_at": target_log.received_at.isoformat(),
                "batch_id": target_batch.id if target_batch else None,
                "policy_version": target_batch.policy_version if target_batch else None,
            }
            if target_log
            else None
        ),
        "timeline": timeline,
        "counterfactual": counter,
        "precision": precision,
        "ledger_mode": mode,
        "cost": cost,
        "cost_note": cost_note,
        "evidence_index": _evidence_index(db, lane, target_log, target_batch, timeline),
    }

    old = _latest_review(db, lane, scope_kind, scope_key) if allow_supersede else None
    version = (old.version + 1) if old else 1
    result["version"] = version
    result = ledger.to_jsonable(result)
    h = ledger.content_hash(result)
    result["content_hash"] = h

    diff = None
    supersedes = None
    if old:
        supersedes = old.version
        diff = _diff(old.result_json or {}, result)
        diff["note"] = "已签发结论保持不变；补传晚到落入已复盘区间，生成新版本与差异说明"
        diff = ledger.to_jsonable(diff)

    rr = ReviewRun(
        lane=lane,
        scope_kind=scope_kind,
        scope_key=scope_key,
        target_receive_log_id=target_log.id if target_log else None,
        window_start=start,
        window_end=end,
        version=version,
        supersedes_version=supersedes,
        status="issued",
        verdict=diagnosis["verdict"] if counter.get("status") != "insufficient" else "insufficient",
        summary=diagnosis["summary"],
        precision=precision,
        cost_note=cost_note,
        result_json=result,
        diff_json=diff,
        issued_by=issued_by,
        content_hash=h,
    )
    db.add(rr)
    db.commit()
    db.refresh(rr)
    return rr


def list_reviews(db, lane: str = ledger.LIVE, limit: int = 50) -> list[dict]:
    rows = (
        db.query(ReviewRun)
        .filter(ReviewRun.lane == lane)
        .order_by(ReviewRun.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "scope_kind": r.scope_kind,
            "scope_key": r.scope_key,
            "version": r.version,
            "supersedes_version": r.supersedes_version,
            "verdict": r.verdict,
            "summary": r.summary,
            "precision": r.precision,
            "window_start": r.window_start.isoformat() if r.window_start else None,
            "window_end": r.window_end.isoformat() if r.window_end else None,
            "issued_at": r.issued_at.isoformat() if r.issued_at else None,
            "content_hash": r.content_hash,
            "has_diff": r.diff_json is not None,
        }
        for r in rows
    ]


def get_review(db, review_id: int) -> dict | None:
    r = db.get(ReviewRun, review_id)
    if r is None:
        return None
    return {
        "id": r.id,
        "lane": r.lane,
        "scope_kind": r.scope_kind,
        "scope_key": r.scope_key,
        "version": r.version,
        "supersedes_version": r.supersedes_version,
        "status": r.status,
        "verdict": r.verdict,
        "summary": r.summary,
        "precision": r.precision,
        "cost_note": r.cost_note,
        "issued_by": r.issued_by,
        "issued_at": r.issued_at.isoformat() if r.issued_at else None,
        "content_hash": r.content_hash,
        "result": r.result_json,
        "diff": r.diff_json,
    }


# ---------------------------------------------------------------------------
# 实时告警 / 区间复盘（生产台账）
# ---------------------------------------------------------------------------

def live_alerts_for_review(db, limit: int = 30) -> list[dict]:
    """列出可复盘的告警及其等待时长（接收→告警）。"""
    rows = db.query(Alert).order_by(Alert.id.desc()).limit(limit).all()
    out = []
    for a in rows:
        log = db.get(ReceiveLog, a.receive_log_id) if a.receive_log_id else None
        if log is None or log.scenario_id is not None:
            continue
        item = db.query(BatchItem).filter(BatchItem.receive_log_id == log.id).first()
        batch = db.get(Batch, item.batch_id) if item else None
        wait_ms = None
        if batch and batch.created_at:
            wait_ms = int((batch.created_at - log.received_at).total_seconds() * 1000)
        out.append(
            {
                "alert_id": a.id,
                "receive_log_id": log.id,
                "device_point": a.device_point,
                "partition_key": log.partition_key,
                "value": log.value,
                "received_at": log.received_at.isoformat(),
                "alert_latency_ms": a.latency_ms,
                "batch_id": batch.id if batch else None,
                "wait_ms": wait_ms,
                "policy_version": batch.policy_version if batch else None,
            }
        )
    return out


def issue_live_review_for_alert(db, alert_id: int, issued_by: str = "operator") -> ReviewRun | None:
    alert = db.get(Alert, alert_id)
    if alert is None or alert.receive_log_id is None:
        return None
    log = db.get(ReceiveLog, alert.receive_log_id)
    if log is None or log.scenario_id is not None:
        return None
    item = db.query(BatchItem).filter(BatchItem.receive_log_id == log.id).first()
    batch = db.get(Batch, item.batch_id) if item else None
    start = log.received_at - timedelta(minutes=2)
    end = (batch.processed_at if batch and batch.processed_at else datetime.utcnow()) + timedelta(seconds=5)
    return issue_review(
        db, ledger.LIVE, "alert", str(alert.id), start, end,
        target_log_id=log.id, issued_by=issued_by,
    )


def issue_live_review_for_interval(db, start: datetime, end: datetime, issued_by: str = "operator") -> ReviewRun:
    """区间复盘：取区间内首个告警/读数作为目标。"""
    alert = (
        db.query(Alert)
        .filter(Alert.received_at >= start, Alert.received_at <= end)
        .order_by(Alert.received_at)
        .first()
    )
    target_log_id = alert.receive_log_id if alert else None
    scope_key = f"{start.isoformat()}~{end.isoformat()}"
    return issue_review(
        db, ledger.LIVE, "interval", scope_key, start, end,
        target_log_id=target_log_id, issued_by=issued_by,
    )


def _evidence_index(db, lane: str, target_log, target_batch, timeline: dict) -> list[dict]:
    """把结论连回具体读数、策略和处理事实：生成可下钻证据条目。"""
    items = []
    if target_log is not None:
        items.append(
            {
                "kind": "reading",
                "ref": f"receive_log:{target_log.id}",
                "label": f"读数 #{target_log.id} {target_log.device_point}={target_log.value}",
                "partition_key": target_log.partition_key,
                "received_at": target_log.received_at.isoformat(),
            }
        )
        concl = db.query(QualityConclusion).filter(QualityConclusion.receive_log_id == target_log.id).first()
        if concl is not None:
            items.append(
                {
                    "kind": "conclusion",
                    "ref": f"conclusion:{concl.id}",
                    "label": f"质量结论 #{concl.id}：{concl.conclusion}",
                    "detail": concl.detail,
                }
            )
        alert = db.query(Alert).filter(Alert.receive_log_id == target_log.id).first()
        if alert is not None:
            items.append(
                {
                    "kind": "alert",
                    "ref": f"alert:{alert.id}",
                    "label": f"告警 #{alert.id} 延迟 {alert.latency_ms}ms",
                }
            )
    if target_batch is not None:
        items.append(
            {
                "kind": "batch",
                "ref": f"batch:{target_batch.id}",
                "label": f"批次 #{target_batch.id} 策略 v{target_batch.policy_version} 条目 {target_batch.item_count}",
                "created_at": target_batch.created_at.isoformat() if target_batch.created_at else None,
            }
        )
    for step in timeline.get("steps", []):
        items.append(
            {
                "kind": "decision",
                "ref": f"event:{step['event_seq']}",
                "label": f"事件 #{step['event_seq']} 选定 {step['selected']}（策略 v{step['policy_version']}）",
                "explain": step.get("explain"),
            }
        )
    for pe in timeline.get("policy_versions", []):
        if pe.get("effective_ts") is not None:
            items.append(
                {
                    "kind": "policy",
                    "ref": f"policy:{pe['version']}",
                    "label": f"策略 v{pe['version']} 生效（序号 {pe['event_seq']}）",
                    "effective_ts": pe["effective_ts"],
                }
            )
    return items


# ---------------------------------------------------------------------------
# 场景索引与查询
# ---------------------------------------------------------------------------

SCENARIO_LIBRARY = {
    "capacity": {
        "key": "capacity",
        "label": "容量耗尽",
        "desc": "单通道在高风险分区积压下被写满，目标批次只能在容量释放后才生成。",
    },
    "low_risk": {
        "key": "low_risk",
        "label": "低风险阻塞",
        "desc": "降级期间低风险分区任务占用通道，高风险目标读数被推迟。",
    },
    "policy_race": {
        "key": "policy_race",
        "label": "策略切换竞争",
        "desc": "策略切换与批次完成同刻，顺序按预登记事件序号裁定，目标等待源于切换竞争。",
    },
    "insufficient": {
        "key": "insufficient",
        "label": "证据不足（保护）",
        "desc": "事件链断裂/缺少当时水位时，比较器只给出证据不足，不以当前队列冒充历史。",
    },
}


def list_scenarios(db) -> list[dict]:
    rows = db.query(ScenarioRun).order_by(ScenarioRun.created_at.desc()).all()
    seen = {}
    out = []
    for r in rows:
        if r.scenario_key in seen:
            continue
        seen[r.scenario_key] = True
        meta = SCENARIO_LIBRARY.get(r.scenario_key, {})
        latest = _latest_review(db, r.lane, "scenario", r.scenario_key)
        out.append(
            {
                "scenario_key": r.scenario_key,
                "label": r.label or meta.get("label"),
                "desc": meta.get("desc", ""),
                "lane": r.lane,
                "version": r.version,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "review_version": latest.version if latest else None,
                "verdict": latest.verdict if latest else None,
            }
        )
    # 固定顺序便于 UI 呈现
    order = {k: i for i, k in enumerate(SCENARIO_LIBRARY)}
    out.sort(key=lambda x: order.get(x["scenario_key"], 99))
    return out


def scenario_detail(db, scenario_key: str) -> dict | None:
    run = (
        db.query(ScenarioRun)
        .filter(ScenarioRun.scenario_key == scenario_key)
        .order_by(ScenarioRun.id.desc())
        .first()
    )
    if run is None:
        return None
    latest = _latest_review(db, run.lane, "scenario", scenario_key)
    history = (
        db.query(ReviewRun)
        .filter(ReviewRun.lane == run.lane, ReviewRun.scope_kind == "scenario", ReviewRun.scope_key == scenario_key)
        .order_by(ReviewRun.version.desc())
        .all()
    )
    return {
        "scenario_key": scenario_key,
        "lane": run.lane,
        "label": run.label,
        "detail": run.detail,
        "latest_review": latest.result_json if latest else None,
        "review_version": latest.version if latest else None,
        "review_status": latest.status if latest else None,
        "diff": latest.diff_json if latest else None,
        "history": [
            {
                "version": r.version,
                "verdict": r.verdict,
                "precision": r.precision,
                "supersedes_version": r.supersedes_version,
                "issued_at": r.issued_at.isoformat() if r.issued_at else None,
                "content_hash": r.content_hash,
                "diff": r.diff_json,
            }
            for r in history
        ],
    }

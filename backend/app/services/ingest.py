import hashlib
import threading
from datetime import datetime

from ..models import Device, GatewaySession, Partition, ReadingConflict, ReceiveLog
from . import decision_ledger as ledger
from .common import maybe_slow

# 同一会话的接入串行化，保证“同会话+同序列”的判定与写入是原子的
_session_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock_for(session_uuid: str) -> threading.Lock:
    with _guard:
        return _session_locks.setdefault(session_uuid, threading.Lock())


def compute_fingerprint(point_code: str, ts: datetime, value: float) -> str:
    """载荷指纹：点位 + 读数时间 + 数值 的 SHA-256 摘要。"""
    payload = f"{point_code}|{ts.isoformat()}|{value}"
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def ingest_readings(db, gateway_code: str, session_uuid: str, readings, note: str = ""):
    """网关读数接入：所有到达先落持久接收日志，再做重传/冲突判定。

    - 同会话 + 同来源序列 + 同指纹  -> duplicate（重传，只确认一次）
    - 同会话 + 同来源序列 + 不同载荷 -> conflict（并列冲突证据，不覆盖已确认读数）
    """
    now = datetime.utcnow()
    sess = db.query(GatewaySession).filter_by(session_uuid=session_uuid).first()
    session_created = False
    if sess is None:
        sess = GatewaySession(gateway_code=gateway_code, session_uuid=session_uuid, note=note)
        db.add(sess)
        session_created = True
    else:
        sess.last_seen_at = now
        if sess.gateway_code != gateway_code:
            sess.note = (sess.note or "") + f" | 网关变更 {sess.gateway_code}->{gateway_code}"
            sess.gateway_code = gateway_code
    db.commit()

    devices = {d.point_code: d for d in db.query(Device).all()}
    results = []
    counts = {"confirmed": 0, "duplicate": 0, "conflict": 0, "rejected": 0}

    with _lock_for(session_uuid):
        for r in readings:
            device = devices.get(r.point_code)
            if device is None:
                results.append({"source_seq": r.source_seq, "status": "rejected", "reason": "未知点位"})
                counts["rejected"] += 1
                continue
            fp = r.fingerprint or compute_fingerprint(r.point_code, r.ts, r.value)
            existing = (
                db.query(ReceiveLog)
                .filter(ReceiveLog.session_uuid == session_uuid, ReceiveLog.source_seq == r.source_seq)
                .order_by(ReceiveLog.id)
                .all()
            )
            raw = {"point_code": r.point_code, "ts": r.ts.isoformat(), "value": r.value, "fingerprint": fp}
            if not existing:
                status, note_txt, first = "confirmed", "", None
            else:
                first = existing[0]
                if first.fingerprint == fp:
                    status, note_txt = "duplicate", f"重传：与日志 #{first.id} 指纹一致，不重复确认"
                else:
                    status, note_txt = "conflict", f"冲突：与日志 #{first.id} 序列相同但载荷不同，并列存证不覆盖"

            log = ReceiveLog(
                gateway_code=gateway_code,
                session_uuid=session_uuid,
                source_seq=r.source_seq,
                fingerprint=fp,
                device_point=r.point_code,
                reading_ts=r.ts,
                value=r.value,
                raw_payload=raw,
                device_status_at_receipt=device.status,
                partition_key=device.partition_key,
                risk_level=device.risk_level,
                status=status,
                note=note_txt,
            )
            db.add(log)
            maybe_slow(db)  # 演示：数据库写入持续变慢
            db.commit()  # 每条到达立即持久化，id 即全中心接收顺序
            db.refresh(log)

            if status == "confirmed":
                part = db.get(Partition, device.partition_key)
                if part is not None and log.id > (part.confirmed_cursor or 0):
                    part.confirmed_cursor = log.id
                db.commit()
                # 读取确认即登记一条不可变接收事件（生效时刻=接收时刻）。
                ledger.record_event(
                    db,
                    "receive",
                    log.received_at or now,
                    lane=ledger.LIVE,
                    partition_key=log.partition_key,
                    receive_log_id=log.id,
                    payload={
                        "device_point": log.device_point,
                        "value": log.value,
                        "risk_level": log.risk_level,
                        "gateway_code": gateway_code,
                    },
                    actor="gateway",
                    note="实时通道接收确认",
                )
                db.commit()
            elif status == "conflict":
                db.add(
                    ReadingConflict(
                        session_uuid=session_uuid,
                        source_seq=r.source_seq,
                        confirmed_log_id=first.id,
                        conflict_log_id=log.id,
                    )
                )
                db.commit()

            counts[status] += 1
            results.append({"source_seq": r.source_seq, "log_id": log.id, "status": status})

    return {"session_created": session_created, "counts": counts, "results": results}

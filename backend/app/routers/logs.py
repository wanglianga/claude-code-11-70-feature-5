from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import GatewaySession, ReceiveLog, User
from ..security import require_user

router = APIRouter(prefix="/api", tags=["logs"])


def log_to_dict(l: ReceiveLog):
    return {
        "id": l.id,
        "gateway_code": l.gateway_code,
        "session_uuid": l.session_uuid,
        "source_seq": l.source_seq,
        "fingerprint": l.fingerprint,
        "device_point": l.device_point,
        "reading_ts": l.reading_ts.isoformat() if l.reading_ts else None,
        "value": l.value,
        "device_status_at_receipt": l.device_status_at_receipt,
        "partition_key": l.partition_key,
        "risk_level": l.risk_level,
        "status": l.status,
        "note": l.note,
        "received_at": l.received_at.isoformat() if l.received_at else None,
    }


@router.get("/logs")
def list_logs(
    session_uuid: str | None = None,
    gateway_code: str | None = None,
    status: str | None = None,
    device_point: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    _: User = Depends(require_user),
):
    q = db.query(ReceiveLog)
    if session_uuid:
        q = q.filter(ReceiveLog.session_uuid == session_uuid)
    if gateway_code:
        q = q.filter(ReceiveLog.gateway_code == gateway_code)
    if status:
        q = q.filter(ReceiveLog.status == status)
    if device_point:
        q = q.filter(ReceiveLog.device_point == device_point)
    total = q.count()
    rows = q.order_by(ReceiveLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [log_to_dict(l) for l in rows]}


@router.get("/logs/{log_id}")
def get_log(log_id: int, db: Session = Depends(get_db), _: User = Depends(require_user)):
    l = db.get(ReceiveLog, log_id)
    if l is None:
        raise HTTPException(404, "日志不存在")
    return log_to_dict(l)


@router.get("/gateways/sessions")
def list_sessions(db: Session = Depends(get_db), _: User = Depends(require_user)):
    rows = db.query(GatewaySession).order_by(GatewaySession.id.desc()).limit(200).all()
    return [
        {
            "id": s.id,
            "gateway_code": s.gateway_code,
            "session_uuid": s.session_uuid,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
            "status": s.status,
            "note": s.note,
        }
        for s in rows
    ]

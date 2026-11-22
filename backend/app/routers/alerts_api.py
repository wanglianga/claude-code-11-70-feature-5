from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Alert, User
from ..security import require_user

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def alert_to_dict(a: Alert):
    return {
        "id": a.id,
        "conclusion_id": a.conclusion_id,
        "receive_log_id": a.receive_log_id,
        "device_point": a.device_point,
        "level": a.level,
        "message": a.message,
        "status": a.status,
        "received_at": a.received_at.isoformat() if a.received_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "latency_ms": a.latency_ms,
        "acked_at": a.acked_at.isoformat() if a.acked_at else None,
        "acked_by": a.acked_by,
    }


@router.get("")
def list_alerts(
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    _: User = Depends(require_user),
):
    q = db.query(Alert)
    if status:
        q = q.filter(Alert.status == status)
    total = q.count()
    rows = q.order_by(Alert.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [alert_to_dict(a) for a in rows]}


@router.post("/{alert_id}/ack")
def ack_alert(alert_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    a = db.get(Alert, alert_id)
    if a is None:
        raise HTTPException(404, "告警不存在")
    if a.status != "acked":
        a.status = "acked"
        a.acked_at = datetime.utcnow()
        a.acked_by = user.username
        db.commit()
    return alert_to_dict(a)

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Device, User
from ..schemas import DeviceIn, DevicePatch
from ..security import require_user

router = APIRouter(prefix="/api/devices", tags=["devices"])


def to_dict(d: Device):
    return {
        "id": d.id,
        "point_code": d.point_code,
        "name": d.name,
        "category": d.category,
        "factor": d.factor,
        "unit": d.unit,
        "sampling_period_sec": d.sampling_period_sec,
        "risk_level": d.risk_level,
        "threshold_min": d.threshold_min,
        "threshold_max": d.threshold_max,
        "partition_key": d.partition_key,
        "status": d.status,
    }


@router.get("")
def list_devices(category: str | None = None, risk_level: str | None = None,
                 db: Session = Depends(get_db), _: User = Depends(require_user)):
    q = db.query(Device)
    if category:
        q = q.filter(Device.category == category)
    if risk_level:
        q = q.filter(Device.risk_level == risk_level)
    return [to_dict(d) for d in q.order_by(Device.id).all()]


@router.post("")
def create_device(body: DeviceIn, db: Session = Depends(get_db), _: User = Depends(require_user)):
    if db.query(Device).filter(Device.point_code == body.point_code).first():
        raise HTTPException(409, "点位编码已存在")
    d = Device(**body.model_dump())
    db.add(d)
    db.commit()
    db.refresh(d)
    return to_dict(d)


@router.patch("/{device_id}")
def patch_device(device_id: int, body: DevicePatch, db: Session = Depends(get_db), _: User = Depends(require_user)):
    d = db.get(Device, device_id)
    if d is None:
        raise HTTPException(404, "设备不存在")
    data = body.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in ("normal", "calibrating", "maintenance"):
        raise HTTPException(400, "状态须为 normal/calibrating/maintenance")
    for k, v in data.items():
        setattr(d, k, v)
    db.commit()
    return to_dict(d)

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..schemas import IngestIn
from ..services.ingest import ingest_readings

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


@router.post("/readings")
def post_readings(
    body: IngestIn,
    db: Session = Depends(get_db),
    x_ingest_token: str | None = Header(default=None),
):
    """网关上报入口（X-Ingest-Token 鉴权）。所有到达先落持久接收日志。"""
    if x_ingest_token != config.INGEST_TOKEN:
        raise HTTPException(401, "接入令牌无效")
    if not body.readings:
        raise HTTPException(400, "readings 不能为空")
    if len(body.readings) > 5000:
        raise HTTPException(400, "单批次最多 5000 条")
    return ingest_readings(db, body.gateway_code, body.session_uuid, body.readings, note=body.note)

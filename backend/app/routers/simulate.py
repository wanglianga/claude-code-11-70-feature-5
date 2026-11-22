from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import SimFlag, User
from ..schemas import SimBackfillIn, SimRealtimeIn, SlowWriteIn
from ..security import require_user
from ..services import simulator
from ..services.common import set_slow_write

router = APIRouter(prefix="/api/simulate", tags=["simulate"])


@router.post("/realtime")
def sim_realtime(body: SimRealtimeIn, db: Session = Depends(get_db), _: User = Depends(require_user)):
    """场景一：实时流（含高风险超标因子）。"""
    return simulator.sim_realtime(db, body.count, body.exceed_ratio)


@router.post("/backfill")
def sim_backfill(body: SimBackfillIn, db: Session = Depends(get_db), _: User = Depends(require_user)):
    """场景二：断网恢复后的补传流（数小时前的读数集中上传）。"""
    return simulator.sim_backfill(db, body.hours, body.step_sec)


@router.post("/conflict")
def sim_conflict(db: Session = Depends(get_db), _: User = Depends(require_user)):
    """场景三：同会话同序列的重传与冲突载荷。"""
    return simulator.sim_conflict(db)


@router.post("/slow-writes")
def sim_slow_writes(body: SlowWriteIn, db: Session = Depends(get_db), _: User = Depends(require_user)):
    """场景四：数据库写入持续变慢（触发调度器降级与恢复）。"""
    flag = set_slow_write(db, body.enabled, body.delay_ms)
    return {"slow_write_enabled": flag.slow_write_enabled, "slow_write_delay_ms": flag.slow_write_delay_ms}


@router.get("/flags")
def sim_flags(db: Session = Depends(get_db), _: User = Depends(require_user)):
    flag = db.get(SimFlag, 1)
    return {
        "slow_write_enabled": bool(flag and flag.slow_write_enabled),
        "slow_write_delay_ms": flag.slow_write_delay_ms if flag else 0,
    }

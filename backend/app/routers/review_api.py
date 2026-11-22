"""容量决策复盘 API：场景序列、决策时间线、反事实比较、证据下钻、策略与成本。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import require_user
from ..services import decision_ledger as ledger
from ..services import review as review_svc
from ..services import review_scenarios as scenarios

router = APIRouter(prefix="/api/review", tags=["review"])


class StrategyQuery(BaseModel):
    response_target_sec: float | None = None
    peak_event_rate: int | None = None
    retention_days: int | None = None


class IntervalQuery(BaseModel):
    start: datetime
    end: datetime
    target_receive_log_id: int | None = None
    scope_key: str | None = None


@router.get("/config")
def review_config(_: User = Depends(require_user)):
    """台账保留模式、成本边界与策略建议（用于复盘页说明重建精度与成本）。"""
    return {
        "ledger_mode": ledger.mode_info(),
        "cost": ledger.cost_profile(),
        "recommendation": ledger.recommend_mode(),
        "scenarios": review_svc.SCENARIO_LIBRARY,
    }


@router.post("/strategy")
def review_strategy(body: StrategyQuery, _: User = Depends(require_user)):
    """按可接受复盘响应时间 / 峰值事件量 / 监管保留期选择模式。"""
    return ledger.recommend_mode(body.response_target_sec, body.peak_event_rate, body.retention_days)


@router.get("/scenarios")
def list_scenarios(db: Session = Depends(get_db), _: User = Depends(require_user)):
    return review_svc.list_scenarios(db)


@router.post("/scenarios/{scenario_key}/build")
def build_scenario(scenario_key: str, force: bool = False, db: Session = Depends(get_db),
                   _: User = Depends(require_user)):
    if scenario_key not in scenarios.BUILDERS:
        raise HTTPException(404, "未知场景")
    try:
        return scenarios.build_scenario(db, scenario_key, force=force)
    except Exception as exc:  # pragma: no cover - 透出构建错误
        db.rollback()
        raise HTTPException(500, f"构建场景失败: {exc}")


@router.post("/scenarios/{scenario_key}/late-event")
def scenario_late_event(scenario_key: str, db: Session = Depends(get_db), _: User = Depends(require_user)):
    """补传晚到落入已复盘区间 → 生成新的复盘版本与差异说明（不改写已签发结论）。"""
    try:
        return scenarios.apply_late_event(db, scenario_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/scenarios/{scenario_key}")
def scenario_detail(scenario_key: str, db: Session = Depends(get_db), _: User = Depends(require_user)):
    detail = review_svc.scenario_detail(db, scenario_key)
    if detail is None:
        raise HTTPException(404, "场景尚未构建，请先构建")
    return detail


@router.post("/scenarios/{scenario_key}/rebuild")
def scenario_rebuild(scenario_key: str, db: Session = Depends(get_db), _: User = Depends(require_user)):
    if scenario_key not in scenarios.BUILDERS:
        raise HTTPException(404, "未知场景")
    return scenarios.rebuild_scenario(db, scenario_key)


@router.get("/reviews")
def list_reviews(lane: str = ledger.LIVE, limit: int = 50, db: Session = Depends(get_db),
                 _: User = Depends(require_user)):
    return review_svc.list_reviews(db, lane=lane, limit=limit)


@router.get("/reviews/{review_id}")
def get_review(review_id: int, db: Session = Depends(get_db), _: User = Depends(require_user)):
    r = review_svc.get_review(db, review_id)
    if r is None:
        raise HTTPException(404, "复盘不存在")
    return r


@router.get("/live/alerts")
def live_alerts(limit: int = 30, db: Session = Depends(get_db), _: User = Depends(require_user)):
    """生产台账中可复盘的告警（含接收→入批等待）。"""
    return review_svc.live_alerts_for_review(db, limit=limit)


@router.post("/live/alerts/{alert_id}")
def review_alert(alert_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    """从指定告警重建复盘（真实台账，不做反事实写回）。"""
    rr = review_svc.issue_live_review_for_alert(db, alert_id, issued_by=user.username)
    if rr is None:
        raise HTTPException(404, "告警不存在或不属于生产台账")
    return review_svc.get_review(db, rr.id)


@router.post("/live/interval")
def review_interval(body: IntervalQuery, db: Session = Depends(get_db), user: User = Depends(require_user)):
    """按延迟区间复盘生产台账。"""
    if body.end <= body.start:
        raise HTTPException(400, "区间结束必须晚于开始")
    rr = review_svc.issue_live_review_for_interval(db, body.start, body.end, issued_by=user.username)
    return review_svc.get_review(db, rr.id)


@router.post("/live/by-reading/{log_id}")
def review_by_reading(log_id: int, scope_end: datetime | None = None, db: Session = Depends(get_db),
                      user: User = Depends(require_user)):
    """从指定读数重建复盘（真实台账，反事实只读计算）。"""
    from ..models import ReceiveLog

    log = db.get(ReceiveLog, log_id)
    if log is None or log.scenario_id is not None:
        raise HTTPException(404, "读数不存在或不属于生产台账")
    from datetime import timedelta

    end = scope_end or (log.received_at + timedelta(hours=1))
    rr = review_svc.issue_review(
        db, ledger.LIVE, "reading", str(log.id), log.received_at - timedelta(minutes=2), end,
        target_log_id=log.id, issued_by=user.username,
    )
    return review_svc.get_review(db, rr.id)

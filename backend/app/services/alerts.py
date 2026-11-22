from datetime import datetime

from ..models import Alert, QualityConclusion, ReceiveLog


def maybe_alert(db, conclusion: QualityConclusion, log: ReceiveLog, device) -> Alert | None:
    """告警服务：只消费 已确认 + 超标 + 高风险 的质量结论。

    设备校准/维护期间产生的 pending_review（未确认）结论不会进入告警。
    """
    if not conclusion.confirmed:
        return None
    if conclusion.conclusion != "exceed":
        return None
    if device is None or device.risk_level != "high":
        return None
    now = datetime.utcnow()
    latency_ms = int((now - log.received_at).total_seconds() * 1000)
    alert = Alert(
        conclusion_id=conclusion.id,
        receive_log_id=log.id,
        device_point=log.device_point,
        level="high",
        message=(
            f"{device.name}（{log.device_point}）{device.factor}={log.value}{device.unit} "
            f"超出阈值 [{device.threshold_min}, {device.threshold_max}]"
        ),
        received_at=log.received_at,
        latency_ms=latency_ms,
    )
    db.add(alert)
    return alert

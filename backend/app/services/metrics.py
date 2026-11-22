import time

from ..models import WriteMetric
from .common import get_state, maybe_slow

# 滞回计数：连续 2 次超阈值进入降级，连续 2 次低于 70% 阈值退出，避免抖动
_over = [0]
_under = [0]


def metrics_tick(db) -> float:
    """数据库写入延迟探针：实测一次写入提交的耗时，并驱动调度器降级/恢复。"""
    t0 = time.perf_counter()
    m = WriteMetric(component="probe", latency_ms=-1)
    db.add(m)
    maybe_slow(db)  # 演示：写入变慢时探针同样被拖慢
    db.commit()
    latency = (time.perf_counter() - t0) * 1000.0
    db.refresh(m)
    m.latency_ms = latency
    db.commit()

    state = get_state(db)
    state.last_latency_ms = latency
    threshold = state.threshold_ms or 800
    if latency > threshold:
        _over[0] += 1
        _under[0] = 0
    elif latency < threshold * 0.7:
        _under[0] += 1
        _over[0] = 0
    else:
        _over[0] = 0
        _under[0] = 0
    if _over[0] >= 2 and not state.degraded:
        # 写入延迟跨过高风险阈值：暂停低风险衍生计算，为高风险分区保留写入额度
        state.degraded = True
        state.low_risk_paused = True
    elif _under[0] >= 2 and state.degraded:
        state.degraded = False
        state.low_risk_paused = False
    db.commit()
    return latency

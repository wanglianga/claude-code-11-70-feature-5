import threading
import time

from ..models import SchedulerState, SimFlag

# 调度器与恢复编排器共用的入批锁：保证一条接收日志只会被放入一个批次
batch_lock = threading.Lock()

_flag_cache = {"at": 0.0, "enabled": False, "delay_ms": 0}


def get_state(db) -> SchedulerState:
    st = db.get(SchedulerState, 1)
    if st is None:
        st = SchedulerState(id=1)
        db.add(st)
        db.commit()
        db.refresh(st)
    return st


def maybe_slow(db):
    """模拟数据库写入变慢：开启后每次持久化前注入延迟（1s 缓存避免每行查库）。"""
    now = time.time()
    if now - _flag_cache["at"] > 1.0:
        flag = db.get(SimFlag, 1)
        _flag_cache["at"] = now
        _flag_cache["enabled"] = bool(flag and flag.slow_write_enabled)
        _flag_cache["delay_ms"] = int(flag.slow_write_delay_ms) if flag else 0
    if _flag_cache["enabled"] and _flag_cache["delay_ms"] > 0:
        time.sleep(_flag_cache["delay_ms"] / 1000.0)


def set_slow_write(db, enabled: bool, delay_ms: int) -> SimFlag:
    flag = db.get(SimFlag, 1)
    if flag is None:
        flag = SimFlag(id=1)
        db.add(flag)
    flag.slow_write_enabled = enabled
    flag.slow_write_delay_ms = delay_ms
    db.commit()
    _flag_cache["at"] = 0.0  # 立即失效缓存
    return flag

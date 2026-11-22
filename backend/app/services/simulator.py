import random
import uuid
from datetime import datetime, timedelta

from .. import config
from ..models import Device
from ..schemas import ReadingIn
from .ingest import ingest_readings


def _gen_value(device: Device, exceed: bool = False) -> float:
    """按设备阈值生成合理读数；exceed=True 时生成超标读数。"""
    lo = device.threshold_min if device.threshold_min is not None else 0.0
    hi = device.threshold_max
    if device.category == "rain_valve":
        return float(random.choice([0, 1]))
    if exceed and hi is not None:
        return round(hi * random.uniform(1.05, 1.5), 3)
    if hi is not None:
        return round(random.uniform(lo, lo + (hi - lo) * 0.8), 3)
    return round(random.uniform(0, 100), 3)


def _send(db, gateway: str, session: str, readings: list[dict]):
    payload = [ReadingIn(**r) for r in readings]
    return ingest_readings(db, gateway, session, payload, note="场景模拟")


def sim_realtime(db, count: int = 30, exceed_ratio: float = 0.3):
    """实时流：各设备以当前时间上报，部分高风险因子超标。"""
    devices = db.query(Device).all()
    session = f"sim-rt-{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    readings = []
    for i in range(count):
        d = random.choice(devices)
        exceed = d.risk_level == "high" and d.category != "rain_valve" and random.random() < exceed_ratio
        readings.append(
            {"point_code": d.point_code, "source_seq": i + 1, "ts": now, "value": _gen_value(d, exceed)}
        )
    return _send(db, "GW-SIM-RT", session, readings)


def sim_backfill(db, hours: float = 2.0, step_sec: int = 60):
    """补传流：网关断网恢复后，把数小时前的缓存读数带新会话一次性上传。"""
    hours = min(max(hours, 0.1), 6.0)
    devices = db.query(Device).all()
    session = f"sim-bf-{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    start = now - timedelta(hours=hours)
    # 保证读数时间都早于补传阈值，进入恢复编排器的回补通道
    end = now - timedelta(seconds=config.BACKFILL_AGE_SEC + 30)
    readings = []
    seq = 0
    t = start
    while t <= end:
        for d in devices:
            seq += 1
            readings.append({"point_code": d.point_code, "source_seq": seq, "ts": t, "value": _gen_value(d)})
        t += timedelta(seconds=step_sec)
    return _send(db, "GW-SIM-BF", session, readings)


def sim_conflict(db):
    """冲突重传：同一会话内同一来源序列先发正常值，再重传同载荷，最后发不同载荷。"""
    device = db.query(Device).filter(Device.point_code == "WW-COD-01").first() or db.query(Device).first()
    session = f"sim-cf-{uuid.uuid4().hex[:8]}"
    ts = datetime.utcnow()
    first_value, conflict_value = 88.0, 97.5

    r1 = _send(db, "GW-SIM-CF", session, [{"point_code": device.point_code, "source_seq": 1, "ts": ts, "value": first_value}])
    r2 = _send(db, "GW-SIM-CF", session, [{"point_code": device.point_code, "source_seq": 1, "ts": ts, "value": first_value}])
    r3 = _send(db, "GW-SIM-CF", session, [{"point_code": device.point_code, "source_seq": 1, "ts": ts, "value": conflict_value}])

    return {
        "session_uuid": session,
        "device_point": device.point_code,
        "confirmed": r1["results"][0],
        "duplicate": r2["results"][0],
        "conflict": r3["results"][0],
        "confirmed_value": first_value,
        "conflict_value": conflict_value,
    }

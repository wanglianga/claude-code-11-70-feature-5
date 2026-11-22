from sqlalchemy.orm import Session

from . import config
from .models import Device, Partition, Policy, SchedulerState, SimFlag, User
from .security import hash_password

DEVICES = [
    # point_code, name, category, factor, unit, period, risk, tmin, tmax, partition
    ("WW-COD-01", "废水总排口 COD", "wastewater", "COD", "mg/L", 60, "high", None, 100, "P-HIGH-1"),
    ("WW-NH3-01", "废水总排口 氨氮", "wastewater", "NH3-N", "mg/L", 60, "high", None, 15, "P-HIGH-1"),
    ("WW-PH-01", "废水总排口 pH", "wastewater", "pH", "", 60, "medium", 6, 9, "P-MED-1"),
    ("WG-SO2-01", "废气排口 SO2", "waste_gas", "SO2", "mg/m3", 60, "high", None, 50, "P-HIGH-2"),
    ("WG-NOX-01", "废气排口 NOx", "waste_gas", "NOx", "mg/m3", 60, "high", None, 100, "P-HIGH-2"),
    ("WG-VOC-01", "废气排口 VOCs", "waste_gas", "VOCs", "mg/m3", 120, "medium", None, 60, "P-MED-1"),
    ("TK-LV-01", "1# 储罐液位", "tank_level", "level", "m", 30, "medium", 0.2, 12, "P-MED-1"),
    ("TK-LV-02", "2# 储罐液位", "tank_level", "level", "m", 30, "medium", 0.2, 12, "P-MED-1"),
    ("TK-LV-03", "3# 储罐液位", "tank_level", "level", "m", 30, "low", 0.2, 12, "P-LOW-1"),
    ("RV-01", "雨污切换阀 1#", "rain_valve", "valve_state", "", 10, "high", 0, 1, "P-HIGH-1"),
    ("RV-02", "雨污切换阀 2#", "rain_valve", "valve_state", "", 10, "low", 0, 1, "P-LOW-1"),
    ("WW-FLOW-01", "废水排口流量", "wastewater", "flow", "m3/h", 60, "low", 0, 500, "P-LOW-1"),
]

PARTITIONS = [
    ("P-HIGH-1", "high", "高风险分区 1（废水超标因子 + 雨污阀）"),
    ("P-HIGH-2", "high", "高风险分区 2（废气超标因子）"),
    ("P-MED-1", "medium", "中风险分区（pH/VOCs/储罐液位）"),
    ("P-LOW-1", "low", "低风险分区（趋势类因子）"),
]


def seed(db: Session):
    if not db.query(User).count():
        db.add_all(
            [
                User(username="admin", password_hash=hash_password("admin123"), role="admin"),
                User(username="operator", password_hash=hash_password("operator123"), role="operator"),
            ]
        )
    if not db.query(Partition).count():
        for key, cls, desc in PARTITIONS:
            db.add(Partition(partition_key=key, risk_class=cls, description=desc))
    if not db.query(Device).count():
        for code, name, cat, factor, unit, period, risk, tmin, tmax, part in DEVICES:
            db.add(
                Device(
                    point_code=code,
                    name=name,
                    category=cat,
                    factor=factor,
                    unit=unit,
                    sampling_period_sec=period,
                    risk_level=risk,
                    threshold_min=tmin,
                    threshold_max=tmax,
                    partition_key=part,
                )
            )
    if not db.query(Policy).count():
        db.add(
            Policy(
                version=1,
                name="初始策略 v1",
                priority_json={"high": 100, "medium": 50, "low": 10},
                reserve_high_writes=50,
                active=True,
                note="系统初始调度策略",
            )
        )
    if db.query(SchedulerState).count() == 0:
        db.add(SchedulerState(id=1, threshold_ms=config.WRITE_LATENCY_THRESHOLD_MS))
    if db.query(SimFlag).count() == 0:
        db.add(SimFlag(id=1))
    db.commit()

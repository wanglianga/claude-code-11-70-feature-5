from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    point_code: str
    source_seq: int
    ts: datetime
    value: float
    fingerprint: Optional[str] = None  # 网关可自带载荷指纹，缺省由中心计算


class IngestIn(BaseModel):
    gateway_code: str
    session_uuid: str
    note: str = ""
    readings: list[ReadingIn] = Field(default_factory=list)


class DevicePatch(BaseModel):
    status: Optional[str] = None  # normal/calibrating/maintenance
    threshold_min: Optional[float] = None
    threshold_max: Optional[float] = None
    risk_level: Optional[str] = None


class DeviceIn(BaseModel):
    point_code: str
    name: str
    category: str
    factor: str
    unit: str = ""
    sampling_period_sec: int = 60
    risk_level: str = "low"
    threshold_min: Optional[float] = None
    threshold_max: Optional[float] = None
    partition_key: str
    status: str = "normal"


class PolicyIn(BaseModel):
    name: str
    priority: dict[str, Any] = Field(default_factory=lambda: {"high": 100, "medium": 50, "low": 10})
    reserve_high_writes: int = 50
    activate: bool = True
    note: str = ""


class RecoveryRunIn(BaseModel):
    partition_key: Optional[str] = None  # 为空表示全部分区


class SlowWriteIn(BaseModel):
    enabled: bool
    delay_ms: int = 1200


class SimRealtimeIn(BaseModel):
    count: int = 30
    exceed_ratio: float = 0.3


class SimBackfillIn(BaseModel):
    hours: float = 2.0
    step_sec: int = 60

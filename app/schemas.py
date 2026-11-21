"""请求 / 响应模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_BATCH = 500
MAX_CHANGES_PER_ORDER = 64


def _clean_part(v):
    """空号代表空位：空串 / 纯空白统一归一为 None。"""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


class Change(BaseModel):
    """单个轴位的换下 / 换上。"""

    position: str = Field(min_length=1, max_length=32, description="轴位，如 A1、B3")
    off_part: Optional[str] = Field(default=None, max_length=64, description="换下件号，空号（null/空串）代表空位")
    on_part: Optional[str] = Field(default=None, max_length=64, description="换上件号，空号（null/空串）代表空位")

    @field_validator("position", mode="before")
    @classmethod
    def _strip_position(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("off_part", "on_part", mode="before")
    @classmethod
    def _empty_is_none(cls, v):
        return _clean_part(v)

    @model_validator(mode="after")
    def _not_both_empty(self):
        if self.off_part is None and self.on_part is None:
            raise ValueError("换下件号与换上件号不能同时为空（该行应表示一次变动）")
        return self


class WorkOrder(BaseModel):
    """换装工单。"""

    order_no: str = Field(min_length=1, max_length=64, description="工单号")
    revision: int = Field(ge=1, le=1_000_000, description="递增修订号，更正时 +1")
    ts: datetime = Field(description="换装发生的 UTC 时刻（ISO 8601，须带时区）")
    car_no: str = Field(min_length=1, max_length=64, description="车号")
    mileage: int = Field(ge=0, le=2_000_000_000, description="该车当时的累计公里数")
    changes: list[Change] = Field(default_factory=list, max_length=MAX_CHANGES_PER_ORDER)

    @field_validator("order_no", "car_no", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("ts")
    @classmethod
    def _must_be_utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("ts 必须携带时区（要求 UTC 时刻，如 2026-11-01T08:00:00Z）")
        return v.astimezone(timezone.utc)


class BatchRequest(BaseModel):
    """一批换装工单：整批校验、整批提交、整批退回。"""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "orders": [
                        {
                            "order_no": "WO-1001",
                            "revision": 1,
                            "ts": "2026-11-01T08:00:00Z",
                            "car_no": "TC01",
                            "mileage": 120000,
                            "changes": [
                                {"position": "A1", "off_part": None, "on_part": "BRG-0001"},
                                {"position": "A2", "off_part": None, "on_part": "BRG-0002"},
                            ],
                        },
                        {
                            "order_no": "WO-1002",
                            "revision": 1,
                            "ts": "2026-11-01T08:00:00Z",
                            "car_no": "TC02",
                            "mileage": 86000,
                            "changes": [
                                {"position": "B1", "off_part": None, "on_part": "BRG-0003"}
                            ],
                        },
                    ]
                }
            ]
        }
    )

    orders: list[WorkOrder] = Field(min_length=1, max_length=MAX_BATCH)


class ConflictError(BaseModel):
    type: str = Field(description="冲突类型代码")
    message: str = Field(description="冲突说明")
    order_no: Optional[str] = None
    car_no: Optional[str] = None
    position: Optional[str] = None
    part_no: Optional[str] = None


class ConflictReport(BaseModel):
    message: str
    errors: list[ConflictError]


class BatchResult(BaseModel):
    applied: list[str] = Field(description="本次新写入（含更正替换）的工单号")
    idempotent: list[str] = Field(description="同号同版同内容、被幂等跳过的工单号")
    replaced: list[str] = Field(description="被更高修订号更正替换的工单号")
    effective_orders: int = Field(description="提交后库内有效工单总数")


class FinishedSegment(BaseModel):
    car_no: str
    position: str
    start_order: str
    start_ts: str
    start_mileage: int
    end_order: str
    end_ts: str
    end_mileage: int
    mileage_delta: int = Field(description="本段里程差（公里）")


class ActiveSegment(BaseModel):
    """在役段只报位置。"""

    car_no: str
    position: str


class PartHistory(BaseModel):
    part_no: str
    total_mileage: int = Field(description="已结束装车段的累计里程（不含在役段）")
    finished_segments: list[FinishedSegment]
    active: Optional[ActiveSegment] = Field(description="在役段位置；不在役为 null")


class PositionState(BaseModel):
    position: str
    part_no: str
    since_order: str
    since_ts: str
    since_mileage: int


class CarState(BaseModel):
    car_no: str
    positions: list[PositionState]


class OrderInfo(BaseModel):
    order_no: str
    revision: int
    ts: str
    car_no: str
    mileage: int
    changes: list[Change]

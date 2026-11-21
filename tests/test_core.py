"""核心重建逻辑的单元测试（不经过 HTTP 层）。"""
from __future__ import annotations

from app.core import (
    MILEAGE_REGRESSION,
    OFF_PART_MISMATCH,
    PART_DOUBLE_INSTALL,
    SAME_TS_CONFLICT,
    rebuild,
)

T1 = "2026-11-01T08:00:00+00:00"
T2 = "2026-11-05T08:00:00+00:00"
T3 = "2026-11-10T08:00:00+00:00"
T4 = "2026-11-15T08:00:00+00:00"


def o(no, ts, car, km, changes):
    return {"order_no": no, "ts": ts, "car_no": car, "mileage": km,
            "changes": [{"position": p, "off_part": off, "on_part": on}
                        for p, off, on in changes]}


def types(errors):
    return {e["type"] for e in errors}


def test_rebuild_swap_same_ts():
    segs, errs = rebuild([
        o("W1", T1, "C1", 100, [("A1", None, "P1")]),
        o("W2", T1, "C1", 100, [("A2", None, "P2")]),
        o("W3", T2, "C1", 200, [("A1", "P1", "P2"), ("A2", "P2", "P1")]),
    ])
    assert errs == []
    open_parts = {s["part_no"] for s in segs if s["end_order"] is None}
    assert open_parts == {"P1", "P2"}
    loc = {s["part_no"]: s["position"] for s in segs if s["end_order"] is None}
    assert loc == {"P1": "A2", "P2": "A1"}


def test_rebuild_reports_multiple_errors():
    segs, errs = rebuild([
        o("W1", T1, "C1", 100, [("A1", None, "P1")]),
        o("W2", T2, "C1", 50, [("A1", "P9", None)]),   # 里程倒退 → 整组跳过
        o("W3", T3, "C2", 10, [("B1", None, "P1")]),   # P1 仍占 C1/A1 → 占两处
        o("W4", T4, "C1", 200, [("A1", "P9", None)]),  # 旧件对不上
    ])
    assert MILEAGE_REGRESSION in types(errs)
    assert PART_DOUBLE_INSTALL in types(errs)
    assert OFF_PART_MISMATCH in types(errs)


def test_rebuild_position_twice_same_ts():
    segs, errs = rebuild([
        o("W1", T1, "C1", 100, [("A1", None, "P1")]),
        o("W2", T1, "C1", 100, [("A1", None, "P2")]),
    ])
    assert types(errs) == {SAME_TS_CONFLICT}


def test_rebuild_off_part_mismatch():
    segs, errs = rebuild([
        o("W1", T1, "C1", 100, [("A1", None, "P1")]),
        o("W2", T2, "C1", 200, [("A1", "PX", None)]),
    ])
    assert OFF_PART_MISMATCH in types(errs)

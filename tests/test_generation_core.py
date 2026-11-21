"""分代归属核心逻辑的单元测试（不经过 HTTP 层）。"""
from __future__ import annotations

from app.core import (
    GENERATION_ORDER_INVERTED,
    GENERATION_ORDER_NOT_FOUND,
    GENERATION_ORDER_NOT_MATCHING,
    GENERATION_SEGMENT_AMBIGUOUS,
    assign_generations,
)

T1 = "2026-11-01T08:00:00+00:00"
T2 = "2026-11-05T08:00:00+00:00"
T3 = "2026-11-10T08:00:00+00:00"
T4 = "2026-11-15T08:00:00+00:00"
T5 = "2026-11-20T08:00:00+00:00"


def o(no, ts, car, km, changes):
    return {"order_no": no, "ts": ts, "car_no": car, "mileage": km,
            "changes": [{"position": p, "off_part": off, "on_part": on}
                        for p, off, on in changes]}


def seg(start_order, start_ts, start_km, end_order, end_ts, end_km, part="P1"):
    return {
        "part_no": part, "car_no": "C1", "position": "A1",
        "start_order": start_order, "start_ts": start_ts, "start_mileage": start_km,
        "end_order": end_order, "end_ts": end_ts, "end_mileage": end_km,
        "generation": None,
    }


def orders_for(part="P1"):
    """P1：T1 装、T2 卸、T3 装、T4 卸、T4 同刻再装（新件）、T5 在役。"""
    return [
        o("W1", T1, "C1", 100, [("A1", None, part)]),
        o("W2", T2, "C1", 200, [("A1", part, None)]),
        o("W3", T3, "C1", 300, [("A1", None, part)]),
        o("W4", T4, "C1", 400, [("A1", part, None), ("A2", None, part)]),
    ]


def test_assign_basic_split_with_same_ts_boundary():
    segments = [
        seg("W1", T1, 100, "W2", T2, 200),
        seg("W3", T3, 300, "W4", T4, 400),
        seg("W4", T4, 400, None, None, None),
    ]
    errs = assign_generations(segments, orders_for(), "P1", "W4", "W4")
    assert errs == []
    assert [s["generation"] for s in segments] == [1, 1, 2]


def test_assign_distinct_ts_boundary():
    """末卸 T2、首装 T3（相邻时刻）：两段分别归一代、二代。"""
    segments = [
        seg("W1", T1, 100, "W2", T2, 200),
        seg("W3", T3, 300, None, None, None),
    ]
    errs = assign_generations(segments, orders_for(), "P1", "W2", "W3")
    assert errs == []
    assert [s["generation"] for s in segments] == [1, 2]


def test_assign_order_not_found():
    segments = [seg("W1", T1, 100, "W2", T2, 200)]
    errs = assign_generations(segments, orders_for(), "P1", "W9", "W3")
    assert {e["type"] for e in errs} == {GENERATION_ORDER_NOT_FOUND}
    assert errs[0]["order_no"] == "W9"


def test_assign_order_not_matching_off_or_on():
    segments = [seg("W1", T1, 100, "W2", T2, 200)]
    # W1 是装车工单，不能当末卸；W2 是卸车工单，不能当首装（时刻 T1<=T2 不倒置）
    errs = assign_generations(segments, orders_for(), "P1", "W1", "W2")
    assert {e["type"] for e in errs} == {GENERATION_ORDER_NOT_MATCHING}
    assert {e["order_no"] for e in errs} == {"W1", "W2"}


def test_assign_inverted_timestamps():
    segments = [
        seg("W1", T1, 100, "W2", T2, 200),
        seg("W3", T3, 300, "W4", T4, 400),
    ]
    # 末卸 W4(T4) 晚于首装 W3(T3)
    errs = assign_generations(segments, orders_for(), "P1", "W4", "W3")
    assert {e["type"] for e in errs} == {GENERATION_ORDER_INVERTED}


def test_assign_gap_segment_is_ambiguous():
    """末卸 T2 与首装 T4 之间有段 W3~W4：W3 段结束于 T4 不晚于 T2 → 两边都不靠。"""
    segments = [
        seg("W1", T1, 100, "W2", T2, 200),
        seg("W3", T3, 300, "W4", T4, 400),   # 间隔段：T3>T2 且 T4>T2，T3<T4
        seg("W4", T4, 400, None, None, None),
    ]
    errs = assign_generations(segments, orders_for(), "P1", "W2", "W4")
    assert len(errs) == 1
    assert errs[0]["type"] == GENERATION_SEGMENT_AMBIGUOUS
    assert errs[0]["start_order"] == "W3" and errs[0]["end_order"] == "W4"
    # 出错的段不打标签
    assert segments[1]["generation"] is None


def test_assign_spanning_segment_is_ambiguous():
    """一段从 T1 跨到 T4，跨越末卸 T2：无法唯一归属。"""
    segments = [
        seg("W1", T1, 100, "W4", T4, 400),
        seg("W4", T4, 400, None, None, None),
    ]
    errs = assign_generations(segments, orders_for(), "P1", "W2", "W4")
    assert {e["type"] for e in errs} == {GENERATION_SEGMENT_AMBIGUOUS}


def test_assign_active_segment_before_new_start_is_ambiguous():
    """在役段开始于首装之前：没有结束时刻可比较，不能归二代。"""
    segments = [
        seg("W1", T1, 100, "W2", T2, 200),
        seg("W3", T3, 300, None, None, None),
    ]
    # 末卸 W2(T2)，首装登记 W4（不存在装车段能对上，且在役段 W3 始于 T3<T4）
    errs = assign_generations(segments, orders_for(), "P1", "W2", "W4")
    assert any(e["type"] == GENERATION_SEGMENT_AMBIGUOUS for e in errs)


def test_assign_only_labels_target_part():
    segments = [
        seg("W1", T1, 100, "W2", T2, 200, part="P1"),
        seg("X1", T1, 100, "X2", T2, 200, part="P2"),
    ]
    errs = assign_generations(segments, orders_for(), "P1", "W2", "W3")
    # W3 是 P1 的装车，P2 段不参与，P1 只有一段归一代 → 二代缺段报错
    assert any(e["type"] == GENERATION_SEGMENT_AMBIGUOUS for e in errs)
    p2 = next(s for s in segments if s["part_no"] == "P2")
    assert p2["generation"] is None

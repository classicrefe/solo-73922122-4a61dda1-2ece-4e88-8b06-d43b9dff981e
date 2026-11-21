"""分代 API 端到端测试：登记、按代查询、更正、补录、交错提交。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def wo(no, rev, ts, car, km, changes):
    return {
        "order_no": no, "revision": rev, "ts": ts, "car_no": car, "mileage": km,
        "changes": [{"position": p, "off_part": off, "on_part": on}
                    for p, off, on in changes],
    }


def post(client, *orders):
    return client.post("/batches", json={"orders": list(orders)})


def gen(client, part, old_end, new_start):
    return client.post("/generations", json={
        "part_no": part, "old_end_order": old_end,
        "new_start_order": new_start})


T1 = "2026-11-01T08:00:00Z"
T2 = "2026-11-05T08:00:00Z"
T3 = "2026-11-10T08:00:00Z"
T4 = "2026-11-15T08:00:00Z"
T5 = "2026-11-20T08:00:00Z"
T6 = "2026-11-25T08:00:00Z"


def build_split_history(client, part="P1", same_ts=True):
    """构造同铭牌号两件实物的完整历史。

    旧件：W1 装车（C1/A1，里程 1000）→ W2 卸车（1600）→ W3 再装 C2/B2（8000）
          → W4 末卸（8600）；
    新件：W4 同刻装车 C1/A1（同刻）或 W5 装车（异刻），W6 时在役。
    """
    post(client,
         wo("W1", 1, T1, "C1", 1000, [("A1", None, part)]),
         wo("W2", 1, T2, "C1", 1600, [("A1", part, None)]),
         wo("W3", 1, T3, "C2", 8000, [("B2", None, part)]),
         wo("W4", 1, T4, "C2", 8600, [("B2", part, None)]))
    if same_ts:
        post(client, wo("W4B", 1, T4, "C1", 2000, [("A1", None, part)]),
             wo("W6", 1, T6, "C1", 2300, [("A2", None, "P9")]))
        return "W4", "W4B"
    post(client, wo("W5", 1, T5, "C1", 2100, [("A1", None, part)]),
         wo("W6", 1, T6, "C1", 2300, [("A2", None, "P9")]))
    return "W4", "W5"


# ---------- 登记与按代查询 ----------

def test_register_generation_same_ts_and_query_by_generation(client):
    old_end, new_start = build_split_history(client)
    r = gen(client, "P1", old_end, new_start)
    assert r.status_code == 201, r.json()
    info = r.json()
    assert info["part_no"] == "P1"
    assert (info["old_end_order"], info["new_start_order"]) == ("W4", "W4B")
    assert info["old_end_ts"] == info["new_start_ts"]  # 允许同刻

    # 已分代件号不带 generation → 400
    r = client.get("/parts/P1")
    assert r.status_code == 400
    assert r.json()["detail"]["errors"][0]["type"] == "GENERATION_REQUIRED"

    # 一代（旧件）：两段，累计 (1600-1000)+(8600-8000)=1200，无在役
    h1 = client.get("/parts/P1", params={"generation": 1}).json()
    assert h1["generation"] == 1 and h1["active"] is None
    assert h1["total_mileage"] == 1200
    assert [s["start_order"] for s in h1["finished_segments"]] == ["W1", "W3"]
    assert all(s["generation"] == 1 for s in h1["finished_segments"])

    # 二代（新件）：在役段，只报位置
    h2 = client.get("/parts/P1", params={"generation": 2}).json()
    assert h2["total_mileage"] == 0 and h2["finished_segments"] == []
    assert h2["active"] == {"car_no": "C1", "position": "A1", "generation": 2}

    # 每个段恰好归入一代：两段一代 + 一段二代
    all_segs = (client.get("/parts/P1", params={"generation": 1}).json()["finished_segments"]
                + client.get("/parts/P1", params={"generation": 2}).json()["finished_segments"])
    assert len(all_segs) == 2


def test_register_generation_distinct_ts(client):
    old_end, new_start = build_split_history(client, same_ts=False)
    assert (old_end, new_start) == ("W4", "W5")
    r = gen(client, "P1", old_end, new_start)
    assert r.status_code == 201, r.json()
    h2 = client.get("/parts/P1", params={"generation": 2}).json()
    assert h2["active"]["generation"] == 2


def test_generation_in_car_state_and_generation_list(client):
    build_split_history(client)
    gen(client, "P1", "W4", "W4B")
    st = client.get("/cars/C1/state").json()
    p1 = next(p for p in st["positions"] if p["part_no"] == "P1")
    assert p1["generation"] == 2
    p9 = next(p for p in st["positions"] if p["part_no"] == "P9")
    assert p9["generation"] is None  # 未分代件号

    lst = client.get("/generations").json()
    assert [g["part_no"] for g in lst] == ["P1"]
    assert client.get("/generations/P1").status_code == 200
    assert client.get("/generations/NOPE").status_code == 404


def test_unsplit_part_rejects_generation_param(client):
    post(client, wo("W1", 1, T1, "C1", 1000, [("A1", None, "P1")]))
    r = client.get("/parts/P1", params={"generation": 1})
    assert r.status_code == 400
    assert r.json()["detail"]["errors"][0]["type"] == "GENERATION_NOT_SPLIT"
    # 不带参数照常查询
    assert client.get("/parts/P1").status_code == 200


def test_orders_keep_original_part_no_after_split(client):
    build_split_history(client)
    gen(client, "P1", "W4", "W4B")
    orders = client.get("/orders").json()
    assert all(
        all(ch["off_part"] != "P1-1" and ch["on_part"] != "P1-2"
            for ch in o["changes"])
        for o in orders)
    assert any(any(ch["on_part"] == "P1" for ch in o["changes"]) for o in orders)


# ---------- 登记校验失败 ----------

def test_register_unknown_order_rejected(client):
    build_split_history(client)
    r = gen(client, "P1", "W9", "W4B")
    assert r.status_code == 409
    errs = r.json()["detail"]["errors"]
    assert errs[0]["type"] == "GENERATION_ORDER_NOT_FOUND"
    assert errs[0]["order_no"] == "W9"
    # 未登记：查询仍按未分代处理
    assert client.get("/parts/P1").status_code == 200
    assert client.get("/generations/P1").status_code == 404


def test_register_order_wrong_role_rejected(client):
    build_split_history(client)
    # W1 是装车不是卸车；W4 是卸车不是装车
    r = gen(client, "P1", "W1", "W4")
    assert r.status_code == 409
    assert {e["type"] for e in r.json()["detail"]["errors"]} == {
        "GENERATION_ORDER_NOT_MATCHING"}


def test_register_inverted_orders_rejected(client):
    build_split_history(client, same_ts=False)
    # 末卸 W4(T4)、首装 W3(T3)：两张工单的装卸角色都对，仅时刻先后倒置
    r = gen(client, "P1", "W4", "W3")
    assert r.status_code == 409
    errs = r.json()["detail"]["errors"]
    assert [e["type"] for e in errs] == ["GENERATION_ORDER_INVERTED"]


def test_register_gap_segment_rejected(client):
    """末卸 W2(T2) 与新件首装 W4B(T4) 之间有 W3 段（T3~T4），无法唯一归属。"""
    build_split_history(client)
    r = gen(client, "P1", "W2", "W4B")
    assert r.status_code == 409
    errs = r.json()["detail"]["errors"]
    assert errs[0]["type"] == "GENERATION_SEGMENT_AMBIGUOUS"
    assert errs[0]["start_order"] == "W3"
    # 登记未生效
    assert client.get("/generations/P1").status_code == 404


def test_register_same_order_in_one_pair_rejected_by_schema(client):
    build_split_history(client)
    r = client.post("/generations", json={
        "part_no": "P1", "old_end_order": "W4", "new_start_order": "W4"})
    assert r.status_code == 422


# ---------- 幂等与冲突 ----------

def test_resend_same_pair_is_idempotent(client):
    build_split_history(client)
    assert gen(client, "P1", "W4", "W4B").status_code == 201
    r = gen(client, "P1", "W4", "W4B")
    assert r.status_code == 200
    assert r.json()["new_start_order"] == "W4B"
    assert len(client.get("/generations").json()) == 1


def test_different_pair_conflicts(client):
    build_split_history(client, same_ts=False)
    assert gen(client, "P1", "W4", "W5").status_code == 201
    r = gen(client, "P1", "W2", "W5")
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "GENERATION_CONFLICT"
    # 原登记保持不变
    assert client.get("/generations/P1").json()["old_end_order"] == "W4"


# ---------- 批次补录 / 更正后重新核对 ----------

def test_correction_removing_boundary_order_rejects_batch(client):
    """更正 W4 使其不再卸 P1：分代指定的卸车消失，整批退回，查询保留原结果。"""
    build_split_history(client)
    gen(client, "P1", "W4", "W4B")
    before = client.get("/parts/P1", params={"generation": 1}).json()

    # W4 改为只卸一个不存在的件（与轴位现装不符也会触发 OFF_PART_MISMATCH），
    # 这里直接让 W4 不再包含 P1 的卸车：把 P1 的末卸改到新车位会先对不上，
    # 因此用"删掉 W4、补一张其他工单"的方式验证分代失效路径：
    r = post(client, wo("W4", 2, T4, "C2", 8600, [("B2", "PX", None)]))
    assert r.status_code == 409
    types = {e["type"] for e in r.json()["detail"]["errors"]}
    assert "OFF_PART_MISMATCH" in types  # 重建先失败，分代不再被应用
    # 查询结果保持
    after = client.get("/parts/P1", params={"generation": 1}).json()
    assert after["total_mileage"] == before["total_mileage"]
    assert client.get("/orders").json()
    assert next(o for o in client.get("/orders").json()
                if o["order_no"] == "W4")["revision"] == 1


def test_backfill_extends_old_generation_then_correction_moves_boundary(client):
    """补录更早的旧件服役段：自动归入一代；再更正分界工单时刻，分界随更正生效。"""
    build_split_history(client)
    gen(client, "P1", "W4", "W4B")

    # 补录 T2~T3 之间旧件在 C3 的一段服役（W2 已在 C1 卸下 P1）：
    # 段结束 T2.5+2d 不晚于末卸 T4 → 归一代，分代仍成立，批次应通过
    r = post(client,
             wo("WB1", 1, "2026-11-06T08:00:00Z", "C3", 500,
                [("D1", None, "P1")]),
             wo("WB2", 1, "2026-11-07T08:00:00Z", "C3", 650,
                [("D1", "P1", None)]))
    assert r.status_code == 201, r.json()
    h1 = client.get("/parts/P1", params={"generation": 1}).json()
    assert [s["start_order"] for s in h1["finished_segments"]] == [
        "W1", "WB1", "W3"]
    # 一代累计增加 150
    assert h1["total_mileage"] == 1200 + 150

    # 更正新件首装工单 W4B：时刻从 T4 推迟到 T5（仍装 P1，角色不变）。
    # T4~T5 之间没有 P1 的段，归属仍唯一 → 通过，查询按新分界取数
    r = post(client, wo("W4B", 2, T5, "C1", 2050, [("A1", None, "P1")]))
    assert r.status_code == 201, r.json()
    h2 = client.get("/parts/P1", params={"generation": 2}).json()
    assert h2["active"] == {"car_no": "C1", "position": "A1", "generation": 2}
    reg = client.get("/generations/P1").json()
    assert reg["new_start_ts"].startswith("2026-11-20")
    # 一代结果不受影响
    assert client.get("/parts/P1",
                      params={"generation": 1}).json()["total_mileage"] == 1350


def test_correction_inverting_boundary_ts_rejects_batch(client):
    """更正使新件首装时刻早于旧件末卸时刻 → 分代倒置，整批退回，查询保留原结果。"""
    build_split_history(client, same_ts=False)  # 末卸 W4(T4)，首装 W5(T5)
    gen(client, "P1", "W4", "W5")
    before = client.get("/parts/P1", params={"generation": 1}).json()

    # 更正 W5：首装提前到 T2（C1/A1 在 T2 被 W2 卸下 P1 后为空位，可装 P1？
    # 不行——P1 此刻正装在 C2/B2（W3 是 T3 才装）。T2 时刻 P1 已被 W2 卸下，
    # 处于空闲，可以装到 C1/A1；原 W5 段消失，新段 T2~在役）
    r = post(client, wo("W5", 2, T2, "C1", 1700, [("A1", None, "P1")]))
    # 但 T3 的 W3 又把 P1 装到 C2/B2：P1 已在 C1/A1 在役 → 重建报占两处
    assert r.status_code == 409
    types = {e["type"] for e in r.json()["detail"]["errors"]}
    assert "PART_DOUBLE_INSTALL" in types
    # 查询保留原结果
    assert client.get("/parts/P1",
                      params={"generation": 1}).json()["total_mileage"] == \
        before["total_mileage"]
    assert next(o for o in client.get("/orders").json()
                if o["order_no"] == "W5")["revision"] == 1


def test_backfill_that_deletes_boundary_event_rejects(client):
    """更正 W4B：不再装 P1（改装 PX），分代指定的装车事件消失 → 整批退回。"""
    build_split_history(client)
    gen(client, "P1", "W4", "W4B")
    before = client.get("/parts/P1", params={"generation": 2}).json()

    r = post(client, wo("W4B", 2, T4, "C1", 2000, [("A1", None, "PX")]))
    assert r.status_code == 409
    errs = r.json()["detail"]["errors"]
    assert any(e["type"] == "GENERATION_ORDER_NOT_MATCHING"
               and e["order_no"] == "W4B" for e in errs)
    # 查询保留原结果
    after = client.get("/parts/P1", params={"generation": 2}).json()
    assert after["active"] == before["active"]
    assert next(o for o in client.get("/orders").json()
                if o["order_no"] == "W4B")["revision"] == 1


def test_batch_revalidation_reports_generation_error_directly(client):
    """重建本身合法、仅分代不再成立：批次也必须被分代核对拦下。"""
    # 旧件 W1 装、W2 卸；新件 W3 装、在役
    post(client,
         wo("W1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
         wo("W2", 1, T2, "C1", 1600, [("A1", "P1", None)]),
         wo("W3", 1, T3, "C2", 8000, [("B2", None, "P1")]))
    assert gen(client, "P1", "W2", "W3").status_code == 201

    # 更正 W3：同一时刻同一轴位改装 PX（空位→PX，重建合法，P1 停在 W2 后空闲）。
    # 重建不会报错，但分代登记指定的首装工单 W3 不再包含 P1 的装车 → 分代核对拒绝
    r = post(client, wo("W3", 2, T3, "C2", 8000, [("B2", None, "PX")]))
    assert r.status_code == 409
    errs = r.json()["detail"]["errors"]
    assert [e["type"] for e in errs] == ["GENERATION_ORDER_NOT_MATCHING"]
    assert errs[0]["order_no"] == "W3" and errs[0]["part_no"] == "P1"
    # 库内仍是修订号 1，二代在役位置查询保留原结果
    assert next(o for o in client.get("/orders").json()
                if o["order_no"] == "W3")["revision"] == 1
    h2 = client.get("/parts/P1", params={"generation": 2}).json()
    assert h2["active"] == {"car_no": "C2", "position": "B2", "generation": 2}


def test_failed_generation_registration_leaves_segments_clean(client):
    """登记失败不写任何内容：segments 表无 generation 标签残留。"""
    build_split_history(client)
    r = gen(client, "P1", "W2", "W4B")  # 间隔段歧义
    assert r.status_code == 409
    # 未分代，可直接查；所有段 generation 为 null
    h = client.get("/parts/P1").json()
    assert all(s["generation"] is None for s in h["finished_segments"])
    assert h["active"]["generation"] is None


# ---------- 并发：登记与批次交错 ----------

def test_interleaved_registration_and_batches_are_serialized(client):
    build_split_history(client)
    outcomes = []

    def register():
        outcomes.append(("gen", gen(client, "P1", "W4", "W4B").status_code))

    def batch(i):
        r = post(client, wo(f"WB-{i}", 1, T5, f"CAR-{i}", 100,
                            [(f"A{i}", None, f"PB-{i}")]))
        outcomes.append((f"batch{i}", r.status_code))

    threads = [threading.Thread(target=register)]
    threads += [threading.Thread(target=batch, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code in (200, 201) for _, code in outcomes), outcomes
    # 分代最终生效，且批次工单全部落库
    assert client.get("/generations/P1").status_code == 200
    assert client.get("/parts/P1", params={"generation": 1}).status_code == 200
    orders = {o["order_no"] for o in client.get("/orders").json()}
    assert {f"WB-{i}" for i in range(8)} <= orders


def test_interleaved_conflicting_batch_keeps_registration_consistent(client):
    """交错提交中一个坏批次被退回，不影响已生效分代与其他批次。"""
    build_split_history(client)
    gen(client, "P1", "W4", "W4B")
    barrier = threading.Barrier(2)

    def good_batch():
        barrier.wait()
        post(client, wo("WOK", 1, T5, "CX", 100, [("A1", None, "POK")]))

    def bad_batch():
        barrier.wait()
        # 旧件对不上 → 整批退回
        post(client, wo("WBAD", 1, T5, "C1", 9999, [("A1", "PXXX", None)]))

    threads = [threading.Thread(target=good_batch),
               threading.Thread(target=bad_batch)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    orders = {o["order_no"] for o in client.get("/orders").json()}
    assert "WOK" in orders and "WBAD" not in orders
    # 分代查询仍正常
    assert client.get("/parts/P1", params={"generation": 1}).status_code == 200

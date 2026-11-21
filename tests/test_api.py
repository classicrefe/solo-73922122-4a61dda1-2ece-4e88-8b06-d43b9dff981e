"""端到端 API 测试：每用例独立临时库。"""
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
    """changes: [(position, off_part, on_part), ...]，None 表示空号。"""
    return {
        "order_no": no, "revision": rev, "ts": ts, "car_no": car, "mileage": km,
        "changes": [{"position": p, "off_part": off, "on_part": on}
                    for p, off, on in changes],
    }


def post(client, *orders):
    return client.post("/batches", json={"orders": list(orders)})


T1 = "2026-11-01T08:00:00Z"
T2 = "2026-11-05T08:00:00Z"
T3 = "2026-11-10T08:00:00Z"
T4 = "2026-11-15T08:00:00Z"


# ---------- 基本流转 ----------

def test_install_then_close_segment(client):
    r = post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
             wo("WO-2", 1, T1, "C2", 5000, [("B1", None, "P2")]))
    assert r.status_code == 201, r.json()
    assert sorted(r.json()["applied"]) == ["WO-1", "WO-2"]

    h = client.get("/parts/P1").json()
    assert h["active"] == {"car_no": "C1", "position": "A1", "generation": None}
    assert h["finished_segments"] == [] and h["total_mileage"] == 0

    r = post(client, wo("WO-3", 1, T2, "C1", 1600, [("A1", "P1", None)]))
    assert r.status_code == 201
    h = client.get("/parts/P1").json()
    assert h["active"] is None
    seg, = h["finished_segments"]
    assert (seg["start_order"], seg["end_order"]) == ("WO-1", "WO-3")
    assert seg["mileage_delta"] == 600 and h["total_mileage"] == 600


def test_active_segment_reports_only_position(client):
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]))
    h = client.get("/parts/P1").json()
    # 在役段只报位置（外加未分代件号恒为 null 的代次标记），不含里程
    assert set(h["active"].keys()) == {"car_no", "position", "generation"}
    assert h["active"]["generation"] is None


def test_swap_between_cars_same_ts_is_atomic(client):
    """同一时刻两车互换轴承：整体判定，应通过。"""
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
         wo("WO-2", 1, T1, "C2", 2000, [("B1", None, "P2")]))
    r = post(client, wo("WO-3", 1, T2, "C1", 1500, [("A1", "P1", "P2")]),
             wo("WO-4", 1, T2, "C2", 2600, [("B1", "P2", "P1")]))
    assert r.status_code == 201, r.json()

    h1 = client.get("/parts/P1").json()
    assert h1["active"] == {"car_no": "C2", "position": "B1", "generation": None}
    assert h1["finished_segments"][0]["mileage_delta"] == 500
    h2 = client.get("/parts/P2").json()
    assert h2["active"] == {"car_no": "C1", "position": "A1", "generation": None}
    assert h2["finished_segments"][0]["mileage_delta"] == 600


def test_move_part_to_other_car_same_ts(client):
    """轴承换到另一台车：里程按件号继续累计。"""
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]))
    r = post(client, wo("WO-2", 1, T2, "C1", 1400, [("A1", "P1", None)]),
             wo("WO-3", 1, T2, "C2", 8000, [("B2", None, "P1")]))
    assert r.status_code == 201, r.json()
    h = client.get("/parts/P1").json()
    assert h["active"] == {"car_no": "C2", "position": "B2", "generation": None}
    assert h["total_mileage"] == 400

    # C2 跑到 8600 时换下：累计 = 400 + 600
    post(client, wo("WO-4", 1, T3, "C2", 8600, [("B2", "P1", None)]))
    h = client.get("/parts/P1").json()
    assert h["total_mileage"] == 1000
    assert len(h["finished_segments"]) == 2


# ---------- 冲突：整批退回 ----------

def test_off_part_mismatch_rejected(client):
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]))
    # 报换下 P9，实际装的是 P1
    r = post(client, wo("WO-2", 1, T2, "C1", 1200, [("A1", "P9", None)]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "OFF_PART_MISMATCH"
    # 空位声明与实际不符（未报换下件）
    r = post(client, wo("WO-2", 1, T2, "C1", 1200, [("A1", None, "P2")]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "OFF_PART_MISMATCH"
    # 状态未被污染
    assert client.get("/parts/P1").json()["active"] == {
        "car_no": "C1", "position": "A1", "generation": None}
    assert len(client.get("/orders").json()) == 1


def test_double_install_rejected(client):
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]))
    r = post(client, wo("WO-2", 1, T2, "C2", 5000, [("B1", None, "P1")]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "PART_DOUBLE_INSTALL"


def test_double_install_within_same_ts_group(client):
    r = post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
             wo("WO-2", 1, T1, "C2", 2000, [("B1", None, "P1")]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "PART_DOUBLE_INSTALL"


def test_mileage_regression_rejected(client):
    post(client, wo("WO-1", 1, T1, "C1", 2000, [("A1", None, "P1")]))
    r = post(client, wo("WO-2", 1, T2, "C1", 1500, [("A2", None, "P2")]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "MILEAGE_REGRESSION"


def test_same_ts_position_changed_twice(client):
    r = post(client,
             wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
             wo("WO-2", 1, T1, "C1", 1000, [("A1", None, "P2")]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "SAME_TS_CONFLICT"


def test_same_ts_same_car_mileage_mismatch(client):
    r = post(client,
             wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
             wo("WO-2", 1, T1, "C1", 1001, [("A2", None, "P2")]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "SAME_TS_CONFLICT"


def test_failed_batch_is_atomic(client):
    """批内一张有效工单 + 一张冲突工单：整批退回，有效工单也不落库。"""
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]))
    r = post(client,
             wo("WO-2", 1, T2, "C2", 3000, [("B1", None, "P2")]),   # 本身有效
             wo("WO-3", 1, T2, "C1", 1200, [("A1", "P9", None)]))   # 旧件对不上
    assert r.status_code == 409
    assert [o["order_no"] for o in client.get("/orders").json()] == ["WO-1"]
    assert client.get("/parts/P2").status_code == 404


# ---------- 幂等与修订号 ----------

def test_idempotent_resend_counts_once(client):
    order = wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")])
    assert post(client, order).status_code == 201
    r = post(client, order)
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == [] and body["idempotent"] == ["WO-1"]
    assert body["effective_orders"] == 1
    # 明细顺序不同也视为同内容
    order2 = wo("WO-2", 1, T2, "C2", 500, [("B2", None, "P3"), ("B1", None, "P2")])
    assert post(client, order2).status_code == 201
    order2["changes"].reverse()
    assert post(client, order2).status_code == 200


def test_same_revision_different_content_rejected(client):
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]))
    r = post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P9")]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "REVISION_CONTENT_MISMATCH"


def test_stale_revision_rejected(client):
    post(client, wo("WO-1", 2, T1, "C1", 1000, [("A1", None, "P1")]))
    r = post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "STALE_REVISION"


def test_correction_rebuilds_history(client):
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
         wo("WO-2", 1, T2, "C1", 1500, [("A1", "P1", None)]))
    assert client.get("/parts/P1").json()["total_mileage"] == 500
    # 更正 WO-1：里程 1000 -> 1200
    r = post(client, wo("WO-1", 2, T1, "C1", 1200, [("A1", None, "P1")]))
    assert r.status_code == 201
    assert r.json()["replaced"] == ["WO-1"]
    h = client.get("/parts/P1").json()
    assert h["total_mileage"] == 300
    assert client.get("/orders").json()[0]["revision"] == 2


def test_correction_causing_conflict_is_rejected(client):
    """更正后旧件对不上：整批退回，历史保持原样。"""
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
         wo("WO-2", 1, T2, "C1", 1500, [("A1", "P1", None)]))
    r = post(client, wo("WO-1", 2, T1, "C1", 1000, [("A1", None, "P9")]))
    assert r.status_code == 409
    assert r.json()["detail"]["errors"][0]["type"] == "OFF_PART_MISMATCH"
    assert client.get("/parts/P1").json()["total_mileage"] == 500


# ---------- 补录 ----------

def test_backfill_inserts_history(client):
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
         wo("WO-3", 1, T3, "C1", 2000, [("A1", "P1", None)]))
    # 补录 T2 时刻另一轴位的换装
    r = post(client, wo("WO-2", 1, T2, "C1", 1400, [("A2", None, "P2")]))
    assert r.status_code == 201, r.json()
    assert client.get("/parts/P1").json()["total_mileage"] == 1000
    assert client.get("/parts/P2").json()["active"] == {
        "car_no": "C1", "position": "A2", "generation": None}


def test_backfill_invalidating_later_order_is_rejected(client):
    """补录把 P1 在 T2 移到 C2 后，T3 的换下工单（在 C1）对不上 → 整批退回。"""
    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]),
         wo("WO-3", 1, T3, "C1", 2000, [("A1", "P1", None)]))
    r = post(client, wo("WO-2", 1, T2, "C1", 1400, [("A1", "P1", None)]),
             wo("WO-2B", 1, T2, "C2", 7000, [("B1", None, "P1")]))
    assert r.status_code == 409
    types = {e["type"] for e in r.json()["detail"]["errors"]}
    assert "OFF_PART_MISMATCH" in types
    assert len(client.get("/orders").json()) == 2  # 未写入


# ---------- 查询与并发 ----------

def test_unknown_part_404(client):
    assert client.get("/parts/NOPE").status_code == 404


def test_car_state(client):
    post(client, wo("WO-1", 1, T1, "C1", 1000,
                    [("A1", None, "P1"), ("A2", None, "P2")]))
    st = client.get("/cars/C1/state").json()
    assert [p["position"] for p in st["positions"]] == ["A1", "A2"]
    assert client.get("/cars/C2/state").json()["positions"] == []


def test_interleaved_submissions_are_serialized(client):
    """多线程交错提交：每批要么完整生效要么完整退回，终态一致。"""
    results, errors = [], []

    def submit(i):
        try:
            r = post(client, wo(f"WO-T{i}", 1, T1, f"CAR-{i}", 100, [(f"A{i}", None, f"P-T{i}")]))
            results.append(r.status_code)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert all(s == 201 for s in results)
    assert len(client.get("/orders").json()) == 12
    for i in range(12):
        h = client.get(f"/parts/P-T{i}").json()
        assert h["active"] == {"car_no": f"CAR-{i}", "position": f"A{i}",
                               "generation": None}


def test_reader_sees_only_committed_state(client, tmp_path):
    """提交中途（事务未提交）读者只能看到最近完整提交的结果。"""
    import sqlite3

    post(client, wo("WO-1", 1, T1, "C1", 1000, [("A1", None, "P1")]))

    db = str(tmp_path / "test.db")
    writer = sqlite3.connect(db, isolation_level=None)
    reader = sqlite3.connect(db)
    reader.row_factory = sqlite3.Row
    try:
        writer.execute("PRAGMA busy_timeout=5000")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO work_orders VALUES ('WO-X', 1, '2026-11-20T00:00:00+00:00',"
            " 'C9', 1, '{}')")
        # 写事务未提交：读者看不到 WO-X
        names = [r["order_no"] for r in
                 reader.execute("SELECT order_no FROM work_orders")]
        assert names == ["WO-1"]
        writer.execute("COMMIT")
        names = [r["order_no"] for r in
                 reader.execute("SELECT order_no FROM work_orders")]
        assert sorted(names) == ["WO-1", "WO-X"]
    finally:
        writer.close()
        reader.close()
    # 清理：该用例直接写了库，删掉脏数据避免影响其他断言
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("DELETE FROM work_orders WHERE order_no='WO-X'")
    conn.close()

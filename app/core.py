"""核心：从空轴位重建换装历史、批次校验与落库。

校验规则
--------
1. 同号工单：同版同内容重送幂等（只计一次）；旧版退回；同版异内容退回；
   更高修订号视为更正，替换旧版后全量重建。
2. 重建按 UTC 时刻分组推进，同一时刻的换下/换上整体判定（允许同刻跨车互换、
   转移），每个轴位在同一时刻最多变动一次。
3. 冲突即整批退回：
   - OFF_PART_MISMATCH   旧件对不上（换下件与轴位现装件不一致）
   - PART_DOUBLE_INSTALL 同一件占两处
   - MILEAGE_REGRESSION  同车里程倒退
   - SAME_TS_CONFLICT    同刻不一致（同轴位重复变动 / 同车同刻里程不一致）
4. 任何失败都不写库；成功时在一个事务内完成工单与派生段的落库，
   查询永远只读到最近完整提交的结果。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timezone

# ---- 冲突类型代码 ----
STALE_REVISION = "STALE_REVISION"
REVISION_CONTENT_MISMATCH = "REVISION_CONTENT_MISMATCH"
DUPLICATE_IN_BATCH = "DUPLICATE_IN_BATCH"
OFF_PART_MISMATCH = "OFF_PART_MISMATCH"
PART_DOUBLE_INSTALL = "PART_DOUBLE_INSTALL"
MILEAGE_REGRESSION = "MILEAGE_REGRESSION"
SAME_TS_CONFLICT = "SAME_TS_CONFLICT"


class BatchRejected(Exception):
    """整批退回。errors 为冲突明细列表（dict）。"""

    def __init__(self, errors: list[dict]):
        super().__init__("batch rejected")
        self.errors = errors


def _err(code: str, message: str, **kw) -> dict:
    e = {"type": code, "message": message}
    e.update({k: v for k, v in kw.items() if v is not None})
    return e


def norm_ts(dt) -> str:
    """统一为 UTC、微秒精度的 ISO 字符串，保证字典序 == 时间序。"""
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def canonical_order(order) -> str:
    """工单规范化 JSON，用于"同号同版同内容"幂等判定。"""
    obj = {
        "order_no": order.order_no,
        "revision": order.revision,
        "ts": norm_ts(order.ts),
        "car_no": order.car_no,
        "mileage": order.mileage,
        "changes": sorted(
            (
                {"position": c.position, "off_part": c.off_part, "on_part": c.on_part}
                for c in order.changes
            ),
            key=lambda d: d["position"],
        ),
    }
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rebuild(orders: list[dict]) -> tuple[list[dict], list[dict]]:
    """从空轴位重放全部有效工单。

    orders: [{"order_no", "ts", "car_no", "mileage",
              "changes": [{"position", "off_part", "on_part"}]}]
    返回 (segments, errors)。errors 非空时 segments 不可用。
    同一时刻的工单为一组整体判定；某组有冲突则跳过该组继续检查后续组，
    以便一次性报出尽量多的冲突。
    """
    errors: list[dict] = []
    finished: list[dict] = []
    state: dict[tuple[str, str], str] = {}   # (车, 轴位) -> 件号
    part_loc: dict[str, tuple[str, str]] = {}  # 件号 -> (车, 轴位)
    open_segs: dict[str, dict] = {}          # 件号 -> 在役段
    car_last: dict[str, tuple[str, int]] = {}  # 车 -> (时刻, 里程)

    groups: dict[str, list[dict]] = {}
    for o in orders:
        groups.setdefault(o["ts"], []).append(o)

    for ts in sorted(groups):
        group = groups[ts]
        gerr: list[dict] = []

        # 1) 同刻每个轴位最多变动一次
        seen: dict[tuple[str, str], str] = {}
        for o in group:
            for ch in o["changes"]:
                key = (o["car_no"], ch["position"])
                if key in seen:
                    gerr.append(_err(
                        SAME_TS_CONFLICT,
                        f"车 {o['car_no']} 轴位 {ch['position']} 在 {ts} 被工单 "
                        f"{seen[key]} 与 {o['order_no']} 重复变动",
                        order_no=o["order_no"], car_no=o["car_no"], position=ch["position"]))
                else:
                    seen[key] = o["order_no"]

        # 2) 里程：同车同刻里程须一致；同车里程不得倒退
        group_car_m: dict[str, int] = {}
        for o in group:
            car, m = o["car_no"], o["mileage"]
            if car in group_car_m and group_car_m[car] != m:
                gerr.append(_err(
                    SAME_TS_CONFLICT,
                    f"车 {car} 在 {ts} 的工单里程不一致（{group_car_m[car]} 与 {m}）",
                    order_no=o["order_no"], car_no=car))
            else:
                group_car_m.setdefault(car, m)
            prev = car_last.get(car)
            if prev is not None and m < prev[1]:
                gerr.append(_err(
                    MILEAGE_REGRESSION,
                    f"车 {car} 工单 {o['order_no']} 里程 {m} 小于该车 "
                    f"{prev[0]} 的 {prev[1]}，里程倒退",
                    order_no=o["order_no"], car_no=car))

        # 3) 换下件与轴位现装件比对
        for o in group:
            for ch in o["changes"]:
                key = (o["car_no"], ch["position"])
                cur = state.get(key)
                if ch["off_part"] == cur:
                    continue
                if cur is None:
                    msg = (f"车 {o['car_no']} 轴位 {ch['position']} 当前为空位，"
                           f"工单 {o['order_no']} 却报换下 {ch['off_part']}")
                elif ch["off_part"] is None:
                    msg = (f"车 {o['car_no']} 轴位 {ch['position']} 现装 {cur}，"
                           f"工单 {o['order_no']} 未报换下件")
                else:
                    msg = (f"车 {o['car_no']} 轴位 {ch['position']} 现装 {cur}，"
                           f"工单 {o['order_no']} 报换下 {ch['off_part']}，旧件对不上")
                gerr.append(_err(OFF_PART_MISMATCH, msg,
                                 order_no=o["order_no"], car_no=o["car_no"],
                                 position=ch["position"], part_no=ch["off_part"]))

        # 4) 换上件唯一性：同刻互换整体判定——本组换下的件视为已释放
        removed = {ch["off_part"] for o in group for ch in o["changes"] if ch["off_part"]}
        ons: dict[str, tuple[dict, dict]] = {}
        for o in group:
            for ch in o["changes"]:
                p = ch["on_part"]
                if p is None:
                    continue
                if p in ons:
                    gerr.append(_err(
                        PART_DOUBLE_INSTALL,
                        f"件 {p} 在 {ts} 被同时装上两处（车 {ons[p][0]['car_no']} "
                        f"轴位 {ons[p][1]['position']} 与车 {o['car_no']} 轴位 {ch['position']}）",
                        order_no=o["order_no"], part_no=p))
                elif p in part_loc and p not in removed:
                    c0, p0 = part_loc[p]
                    gerr.append(_err(
                        PART_DOUBLE_INSTALL,
                        f"件 {p} 已装于车 {c0} 轴位 {p0}，不能再同时装到车 "
                        f"{o['car_no']} 轴位 {ch['position']}（同一件占两处）",
                        order_no=o["order_no"], part_no=p))
                else:
                    ons[p] = (o, ch)

        # 任一检查未过：整组不应用，继续检查后续时刻以报出更多冲突
        if gerr:
            errors.extend(gerr)
            continue

        # 5) 应用本组：先全部换下，再全部换上
        for o in group:
            for ch in o["changes"]:
                key = (o["car_no"], ch["position"])
                cur = state.pop(key, None)
                if cur is not None:
                    del part_loc[cur]
                    seg = open_segs.pop(cur)
                    seg["end_order"] = o["order_no"]
                    seg["end_ts"] = ts
                    seg["end_mileage"] = o["mileage"]
                    finished.append(seg)
        for p, (o, ch) in ons.items():
            key = (o["car_no"], ch["position"])
            state[key] = p
            part_loc[p] = key
            open_segs[p] = {
                "part_no": p, "car_no": o["car_no"], "position": ch["position"],
                "start_order": o["order_no"], "start_ts": ts,
                "start_mileage": o["mileage"],
                "end_order": None, "end_ts": None, "end_mileage": None,
            }
        for car, m in group_car_m.items():
            car_last[car] = (ts, m)

    return finished + list(open_segs.values()), errors


def _order_to_row_dict(order) -> dict:
    return {
        "order_no": order.order_no,
        "ts": norm_ts(order.ts),
        "car_no": order.car_no,
        "mileage": order.mileage,
        "changes": [
            {"position": c.position, "off_part": c.off_part, "on_part": c.on_part}
            for c in order.changes
        ],
    }


def apply_batch(conn: sqlite3.Connection, orders: list) -> dict:
    """校验并落库一个批次。成功返回统计；冲突抛 BatchRejected，且不写任何内容。"""
    # ---- 批内去重：同号同内容视为重复提交，同号异内容退回 ----
    by_no: dict[str, tuple] = {}
    errors: list[dict] = []
    for o in orders:
        canon = canonical_order(o)
        if o.order_no in by_no:
            if by_no[o.order_no][1] != canon:
                errors.append(_err(DUPLICATE_IN_BATCH,
                                   f"工单 {o.order_no} 在本批内出现多次且内容不一致",
                                   order_no=o.order_no))
        else:
            by_no[o.order_no] = (o, canon)
    if errors:
        raise BatchRejected(errors)

    # ---- 修订号 / 幂等判定 ----
    stored = {r["order_no"]: r for r in
              conn.execute("SELECT order_no, revision, content FROM work_orders")}
    to_write: list[tuple] = []   # (order, canonical)
    idempotent: list[str] = []
    replaced: list[str] = []
    for no, (o, canon) in by_no.items():
        r = stored.get(no)
        if r is None:
            to_write.append((o, canon))
        elif o.revision < r["revision"]:
            errors.append(_err(STALE_REVISION,
                               f"工单 {no} 修订号 {o.revision} 低于已存版本 "
                               f"{r['revision']}，旧版退回", order_no=no))
        elif o.revision == r["revision"]:
            if canon == r["content"]:
                idempotent.append(no)  # 同号同版同内容：只计一次
            else:
                errors.append(_err(REVISION_CONTENT_MISMATCH,
                                   f"工单 {no} 修订号 {o.revision} 与已存版本相同"
                                   f"但内容不一致，同版异内容退回", order_no=no))
        else:
            to_write.append((o, canon))  # 更高修订号：更正
            replaced.append(no)
    if errors:
        raise BatchRejected(errors)

    if not to_write:
        return {"applied": [], "idempotent": sorted(idempotent), "replaced": []}

    # ---- 汇总有效工单：库内未被替换的 + 本批新写入/更正的 ----
    replacing = {o.order_no for o, _ in to_write}
    effective: list[dict] = []
    for r in conn.execute("SELECT order_no, ts, car_no, mileage FROM work_orders"):
        if r["order_no"] in replacing:
            continue
        chs = conn.execute(
            "SELECT position, off_part, on_part FROM order_changes WHERE order_no = ?",
            (r["order_no"],)).fetchall()
        effective.append({
            "order_no": r["order_no"], "ts": r["ts"], "car_no": r["car_no"],
            "mileage": r["mileage"], "changes": [dict(c) for c in chs],
        })
    for o, _ in to_write:
        effective.append(_order_to_row_dict(o))

    # ---- 从空轴位全量重建并校验 ----
    segments, rebuild_errors = rebuild(effective)
    if rebuild_errors:
        raise BatchRejected(rebuild_errors)

    # ---- 单事务落库：查询侧只见完整提交 ----
    conn.execute("BEGIN IMMEDIATE")
    try:
        for o, canon in to_write:
            conn.execute("DELETE FROM work_orders WHERE order_no = ?", (o.order_no,))
            conn.execute(
                "INSERT INTO work_orders (order_no, revision, ts, car_no, mileage, content)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (o.order_no, o.revision, norm_ts(o.ts), o.car_no, o.mileage, canon))
            conn.executemany(
                "INSERT INTO order_changes (order_no, position, off_part, on_part)"
                " VALUES (?, ?, ?, ?)",
                [(o.order_no, ch.position, ch.off_part, ch.on_part) for ch in o.changes])
        conn.execute("DELETE FROM segments")
        conn.executemany(
            "INSERT INTO segments (part_no, car_no, position, start_order, start_ts,"
            " start_mileage, end_order, end_ts, end_mileage) VALUES (?,?,?,?,?,?,?,?,?)",
            [(s["part_no"], s["car_no"], s["position"], s["start_order"], s["start_ts"],
              s["start_mileage"], s["end_order"], s["end_ts"], s["end_mileage"])
             for s in segments])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    return {
        "applied": sorted(o.order_no for o, _ in to_write),
        "idempotent": sorted(idempotent),
        "replaced": sorted(replaced),
    }

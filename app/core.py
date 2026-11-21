"""核心：从空轴位重建换装历史、批次校验与落库、同铭牌号分代。

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

分代规则（同一铭牌号对应两件实物）
----------------------------------
数据员登记旧件最后一次卸车工单 U 与新件第一次装车工单 I：
- U、I 都须在当前有效工单中，且分别包含该件号的一次换下 / 换上；
- U 的时刻不晚于 I 的时刻（允许同刻，工单仍为两张）；
- 段结束时刻 <= U 时刻的段归旧件（一代），段开始时刻 >= I 时刻的段归新件（二代），
  每段须恰好归入一代；间隔段、跨分界段、在役且早于首装的段均判
  GENERATION_SEGMENT_AMBIGUOUS 拒绝登记。
- 批次补录 / 更正后用重建段重新核对全部分代登记，任一不过整批退回，
  查询保留原结果；登记与批次共用写锁，交错提交按"将要生效"的合并状态判定。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

# ---- 冲突类型代码 ----
STALE_REVISION = "STALE_REVISION"
REVISION_CONTENT_MISMATCH = "REVISION_CONTENT_MISMATCH"
DUPLICATE_IN_BATCH = "DUPLICATE_IN_BATCH"
OFF_PART_MISMATCH = "OFF_PART_MISMATCH"
PART_DOUBLE_INSTALL = "PART_DOUBLE_INSTALL"
MILEAGE_REGRESSION = "MILEAGE_REGRESSION"
SAME_TS_CONFLICT = "SAME_TS_CONFLICT"

# ---- 分代相关错误代码 ----
GENERATION_ORDER_NOT_FOUND = "GENERATION_ORDER_NOT_FOUND"
# 指定工单虽存在，但不是该件号的一次"卸车"（旧件末卸）或"装车"（新件首装）
GENERATION_ORDER_NOT_MATCHING = "GENERATION_ORDER_NOT_MATCHING"
# 旧件末卸时刻晚于新件首装时刻（先后倒置）
GENERATION_ORDER_INVERTED = "GENERATION_ORDER_INVERTED"
# 某装车段既不归旧件也不归新件（间隔段 / 跨分界段），无法唯一归属
GENERATION_SEGMENT_AMBIGUOUS = "GENERATION_SEGMENT_AMBIGUOUS"
# 同件号已登记另一对工单
GENERATION_CONFLICT = "GENERATION_CONFLICT"
# 已分代件号查询时未指定代次
GENERATION_REQUIRED = "GENERATION_REQUIRED"
# 未分代件号查询时给了代次
GENERATION_NOT_SPLIT = "GENERATION_NOT_SPLIT"


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


# ---- 分代 ----

def assign_generations(segments: list[dict], orders: list[dict],
                       part_no: str, old_end_order: str,
                       new_start_order: str) -> list[dict]:
    """校验分代登记，并把代次（1/2）写回该件号全部装车段的 generation 字段。

    规则：
    - 两张工单须存在于有效工单中；
    - old_end_order 须包含 part_no 的一次换下（off_part == part_no），
      new_start_order 须包含 part_no 的一次换上（on_part == part_no）；
    - 末卸时刻 <= 首装时刻（允许同刻）；
    - 段 end_ts <= 末卸时刻 → 一代；段 start_ts >= 首装时刻 → 二代；
      其余段（间隔段 / 跨分界段 / 在役段时间不匹配）无法唯一归属，报错。
    成功返回该件号带 generation 的段（已就地修改入参中的同名字典）。
    """
    errors: list[dict] = []
    order_ts = {o["order_no"]: o["ts"] for o in orders}

    for no, role, code in (
        (old_end_order, "旧件末卸", GENERATION_ORDER_NOT_FOUND),
        (new_start_order, "新件首装", GENERATION_ORDER_NOT_FOUND),
    ):
        if no not in order_ts:
            errors.append(_err(
                code,
                f"件号 {part_no} 的{role}工单 {no} 不在当前有效装车工单中，"
                f"无法在有效装车段中定位该次装卸",
                part_no=part_no, order_no=no))
    if errors:
        return errors  # type: ignore[return-value]

    old_ts = order_ts[old_end_order]
    new_ts = order_ts[new_start_order]

    off_orders = {o["order_no"] for o in orders
                  if any(c["off_part"] == part_no for c in o["changes"])}
    on_orders = {o["order_no"] for o in orders
                 if any(c["on_part"] == part_no for c in o["changes"])}
    if old_end_order not in off_orders:
        errors.append(_err(
            GENERATION_ORDER_NOT_MATCHING,
            f"工单 {old_end_order} 不包含件号 {part_no} 的任何卸车，"
            f"不能作为旧件最后一次卸车工单",
            part_no=part_no, order_no=old_end_order))
    if new_start_order not in on_orders:
        errors.append(_err(
            GENERATION_ORDER_NOT_MATCHING,
            f"工单 {new_start_order} 不包含件号 {part_no} 的任何装车，"
            f"不能作为新件第一次装车工单",
            part_no=part_no, order_no=new_start_order))
    if old_ts > new_ts:
        errors.append(_err(
            GENERATION_ORDER_INVERTED,
            f"件号 {part_no} 旧件末卸工单 {old_end_order}（{old_ts}）晚于"
            f"新件首装工单 {new_start_order}（{new_ts}），先后倒置",
            part_no=part_no, order_no=old_end_order))
    if errors:
        return errors  # type: ignore[return-value]

    part_segments = [s for s in segments if s["part_no"] == part_no]
    seen_gen: set[int] = set()
    for s in part_segments:
        in_old = s["end_order"] is not None and s["end_ts"] <= old_ts
        in_new = s["start_ts"] >= new_ts
        if in_old and not in_new:
            s["generation"] = 1
        elif in_new and not in_old:
            s["generation"] = 2
        else:
            s["generation"] = None
            if s["end_order"] is None:
                detail = "该段为在役段，无结束时刻，无法与新件首装时刻比较"
            else:
                detail = (f"段 {s['start_order']}~{s['end_order']}"
                          f"（{s['start_ts']} ~ {s['end_ts']}）跨越或落在"
                          f"末卸 {old_ts} 与首装 {new_ts} 的分界之外，"
                          f"既不满足结束不晚于末卸，也不满足开始不早于首装")
            errors.append(_err(
                GENERATION_SEGMENT_AMBIGUOUS,
                f"件号 {part_no} 的装车段无法唯一归属代次：{detail}",
                part_no=part_no, start_order=s["start_order"],
                end_order=s["end_order"]))
            continue
        seen_gen.add(s["generation"])

    # 防御性兜底：一代/二代都应至少有一段
    for gen, no in ((1, old_end_order), (2, new_start_order)):
        if gen not in seen_gen:
            errors.append(_err(
                GENERATION_SEGMENT_AMBIGUOUS,
                f"件号 {part_no} 没有任何装车段可归入第 {gen} 代"
                f"（参考工单 {no}），分代不成立",
                part_no=part_no, order_no=no, generation=gen))
    return errors  # type: ignore[return-value]


def label_segments_for_registrations(segments: list[dict], orders: list[dict],
                                     registrations: list[dict]) -> list[dict]:
    """用全部分代登记给重建后的装车段打代次标签。

    未分代件号的段 generation 置 None；任一登记在当前重建段上不再成立时
    返回错误列表（调用方须整批退回，保留原查询结果）。
    """
    for s in segments:
        s["generation"] = None
    errors: list[dict] = []
    for r in registrations:
        errs = assign_generations(
            segments, orders, r["part_no"],
            r["old_end_order"], r["new_start_order"])
        errors.extend(errs)
    # 未登记的件号保持 None；登记失败的件号其段标签不可信，整体退回即可
    return errors


def load_effective_orders(conn: sqlite3.Connection, skip: set[str] | None = None) -> list[dict]:
    """读取库内当前有效工单（可跳过将被本批替换的工单号）。"""
    skip = skip or set()
    effective: list[dict] = []
    for r in conn.execute("SELECT order_no, ts, car_no, mileage FROM work_orders"
                          " ORDER BY ts, order_no"):
        if r["order_no"] in skip:
            continue
        chs = conn.execute(
            "SELECT position, off_part, on_part FROM order_changes WHERE order_no = ?"
            " ORDER BY position", (r["order_no"],)).fetchall()
        effective.append({
            "order_no": r["order_no"], "ts": r["ts"], "car_no": r["car_no"],
            "mileage": r["mileage"], "changes": [dict(c) for c in chs],
        })
    return effective


def load_registrations(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT part_no, old_end_order, new_start_order, old_end_ts, new_start_ts,"
        " created_at FROM part_generations ORDER BY part_no")]


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
    effective = load_effective_orders(conn, skip=replacing)
    for o, _ in to_write:
        effective.append(_order_to_row_dict(o))

    # ---- 从空轴位全量重建并校验 ----
    segments, rebuild_errors = rebuild(effective)
    if rebuild_errors:
        raise BatchRejected(rebuild_errors)

    # ---- 用重建段重新核对全部分代登记；任一不再成立则整批退回 ----
    registrations = load_registrations(conn)
    gen_errors = label_segments_for_registrations(segments, effective, registrations)
    if gen_errors:
        raise BatchRejected(gen_errors)

    # 登记仍成立：若分界工单在本批被更正（工单号不变、时刻变化），同步快照
    order_ts = {o["order_no"]: o["ts"] for o in effective}
    snapshot_updates = []
    for r in registrations:
        snapshot_updates.append((
            order_ts[r["old_end_order"]], order_ts[r["new_start_order"]],
            r["part_no"]))

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
        conn.executemany(
            "UPDATE part_generations SET old_end_ts = ?, new_start_ts = ?"
            " WHERE part_no = ?", snapshot_updates)
        conn.execute("DELETE FROM segments")
        conn.executemany(
            "INSERT INTO segments (part_no, car_no, position, start_order, start_ts,"
            " start_mileage, end_order, end_ts, end_mileage, generation)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(s["part_no"], s["car_no"], s["position"], s["start_order"], s["start_ts"],
              s["start_mileage"], s["end_order"], s["end_ts"], s["end_mileage"],
              s.get("generation"))
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


class GenerationRejected(Exception):
    """分代登记被拒绝。errors 为冲突明细列表（dict），库内不做任何改动。"""

    def __init__(self, errors: list[dict]):
        super().__init__("generation registration rejected")
        self.errors = errors


def apply_generation_registration(conn: sqlite3.Connection, req) -> dict:
    """登记 / 幂等返回一条分代记录。

    校验全部基于"将要生效"的有效工单与重建装车段；失败抛 GenerationRejected，
    不写任何内容。同件号重送同一对工单返回原登记；换一对报 GENERATION_CONFLICT。
    """
    stored = conn.execute(
        "SELECT * FROM part_generations WHERE part_no = ?", (req.part_no,)).fetchone()
    if stored is not None:
        if (stored["old_end_order"] == req.old_end_order
                and stored["new_start_order"] == req.new_start_order):
            return {"idempotent": True, "registration": dict(stored)}
        raise GenerationRejected([_err(
            GENERATION_CONFLICT,
            f"件号 {req.part_no} 已登记分代（旧件末卸 {stored['old_end_order']} /"
            f" 新件首装 {stored['new_start_order']}），与本次提交的"
            f"（{req.old_end_order} / {req.new_start_order}）不一致；"
            f"分代登记不可改写，换一对工单即冲突",
            part_no=req.part_no,
            order_no=req.old_end_order)])

    orders = load_effective_orders(conn)
    segments, rebuild_errors = rebuild(orders)
    if rebuild_errors:
        # 理论上不会发生：落库前批次已校验；防御性处理，避免在坏状态上登记
        raise GenerationRejected(rebuild_errors)

    gen_errors = assign_generations(
        segments, orders, req.part_no, req.old_end_order, req.new_start_order)
    if gen_errors:
        raise GenerationRejected(gen_errors)

    old_ts = next(o["ts"] for o in orders if o["order_no"] == req.old_end_order)
    new_ts = next(o["ts"] for o in orders if o["order_no"] == req.new_start_order)
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")

    # 单事务：登记与派生段代次标签一起生效，查询只见完整提交
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO part_generations (part_no, old_end_order, new_start_order,"
            " old_end_ts, new_start_ts, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (req.part_no, req.old_end_order, req.new_start_order,
             old_ts, new_ts, created_at))
        conn.execute("DELETE FROM segments")
        conn.executemany(
            "INSERT INTO segments (part_no, car_no, position, start_order, start_ts,"
            " start_mileage, end_order, end_ts, end_mileage, generation)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(s["part_no"], s["car_no"], s["position"], s["start_order"], s["start_ts"],
              s["start_mileage"], s["end_order"], s["end_ts"], s["end_mileage"],
              s.get("generation"))
             for s in segments])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    return {"idempotent": False, "registration": {
        "part_no": req.part_no,
        "old_end_order": req.old_end_order,
        "new_start_order": req.new_start_order,
        "old_end_ts": old_ts,
        "new_start_ts": new_ts,
        "created_at": created_at,
    }}

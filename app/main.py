"""FastAPI 入口：批量接收换装工单、按件号核算服役里程。"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse

from . import core
from .db import connect, init_db
from .schemas import (
    ActiveSegment,
    BatchRequest,
    BatchResult,
    CarState,
    ConflictReport,
    FinishedSegment,
    GenerationInfo,
    GenerationRequest,
    OrderInfo,
    PartHistory,
    PositionState,
)

# 写操作串行化：批次校验（读库）+ 落库（写库）必须整体互斥，
# 保证交错提交时每个批次看到的都是最近完整提交的状态。
_write_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    try:
        init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="轴承换装里程核算",
    version="1.1.0",
    lifespan=lifespan,
    description=(
        "检修数仓侧核算 API：批量接收换装工单，从空轴位重建换装历史，"
        "按件号累计服役里程（轴承换车后里程连续累计）。\n\n"
        "- 批次整批校验、整批提交、整批退回\n"
        "- 同号同版同内容重送幂等；旧版 / 同版异内容退回；更高修订号为更正\n"
        "- 同一时刻的换下换上整体判定，每轴位每刻最多变动一次\n"
        "- 查询只读最近完整提交的结果\n\n"
        "**分代**：供应商把同一铭牌号用在两件轴承上时，数据员用 "
        "`POST /generations` 登记旧件最后一次卸车与新件第一次装车的工单；"
        "登记后该件号查询装车段、累计里程与在役位置都必须指定代次 "
        "（`generation=1` 旧件、`generation=2` 新件）。"
    ),
)


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.post(
    "/batches",
    response_model=BatchResult,
    responses={
        201: {"description": "批次已接受并提交"},
        200: {"description": "批内工单均已存在（同号同版同内容），幂等跳过"},
        409: {"model": ConflictReport, "description": "批次冲突，整批退回，未写入任何内容"},
    },
    summary="批量提交换装工单",
)
def submit_batch(batch: BatchRequest, response: Response, conn=Depends(get_conn)):
    with _write_lock:
        try:
            result = core.apply_batch(conn, batch.orders)
        except core.BatchRejected as e:
            raise HTTPException(
                status_code=409,
                detail={"message": "批次存在冲突，已整批退回，未写入任何内容",
                        "errors": e.errors},
            )
        result["effective_orders"] = conn.execute(
            "SELECT COUNT(*) AS n FROM work_orders").fetchone()["n"]
    response.status_code = 200 if not result["applied"] else 201
    return result


@app.get(
    "/parts/{part_no}",
    response_model=PartHistory,
    responses={
        404: {"description": "件号无装车记录"},
        400: {"model": ConflictReport, "description": "已分代件号未指定代次，或未分代件号给了代次"},
    },
    summary="按件号（已分代须带 generation）查询装车段与累计服役里程",
)
def part_history(
    part_no: str,
    generation: int | None = Query(
        default=None,
        description="代次：1=旧件，2=新件。已分代件号必须指定，未分代件号不得指定。",
        ge=1, le=2),
    conn=Depends(get_conn),
):
    reg = conn.execute(
        "SELECT 1 FROM part_generations WHERE part_no = ?", (part_no,)).fetchone()
    if reg is not None and generation is None:
        raise HTTPException(status_code=400, detail={
            "message": f"件号 {part_no} 已分代，查询装车段 / 累计里程必须指定代次"
                       f"（generation=1 旧件，generation=2 新件）",
            "errors": [{"type": core.GENERATION_REQUIRED,
                        "message": "缺少查询参数 generation",
                        "part_no": part_no}]})
    if reg is None and generation is not None:
        raise HTTPException(status_code=400, detail={
            "message": f"件号 {part_no} 尚未分代，不接受 generation 参数",
            "errors": [{"type": core.GENERATION_NOT_SPLIT,
                        "message": f"未分代件号不能按第 {generation} 代查询",
                        "part_no": part_no, "generation": generation}]})

    rows = conn.execute(
        "SELECT * FROM segments WHERE part_no = ? ORDER BY start_ts, start_order",
        (part_no,)).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"件号 {part_no} 无装车记录")
    if generation is not None:
        rows = [r for r in rows if r["generation"] == generation]
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"件号 {part_no} 第 {generation} 代无装车记录")
    finished: list[FinishedSegment] = []
    active: ActiveSegment | None = None
    total = 0
    for r in rows:
        if r["end_order"] is None:
            active = ActiveSegment(car_no=r["car_no"], position=r["position"],
                                   generation=r["generation"])
        else:
            delta = r["end_mileage"] - r["start_mileage"]
            total += delta
            finished.append(FinishedSegment(
                car_no=r["car_no"], position=r["position"],
                generation=r["generation"],
                start_order=r["start_order"], start_ts=r["start_ts"],
                start_mileage=r["start_mileage"],
                end_order=r["end_order"], end_ts=r["end_ts"],
                end_mileage=r["end_mileage"], mileage_delta=delta))
    return PartHistory(part_no=part_no, generation=generation,
                       total_mileage=total,
                       finished_segments=finished, active=active)


@app.get(
    "/cars/{car_no}/state",
    response_model=CarState,
    summary="查询车辆当前轴位装件（已分代件号标注代次）",
)
def car_state(car_no: str, conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT s.position, s.part_no, s.start_order, s.start_ts, s.start_mileage,"
        " s.generation FROM segments s"
        " WHERE s.car_no = ? AND s.end_order IS NULL ORDER BY s.position",
        (car_no,)).fetchall()
    return CarState(car_no=car_no, positions=[PositionState(
        position=r["position"], part_no=r["part_no"], generation=r["generation"],
        since_order=r["start_order"], since_ts=r["start_ts"],
        since_mileage=r["start_mileage"]) for r in rows])


@app.post(
    "/generations",
    response_model=GenerationInfo,
    status_code=201,
    responses={
        201: {"description": "分代登记已接受，装车段已按新分界重新打标"},
        200: {"description": "同件号同对工单重送，幂等返回原登记"},
        409: {"model": ConflictReport,
              "description": "登记被拒绝（工单找不到 / 不是该件的装卸 / 先后倒置 /"
                             " 段无法唯一归属 / 同件号另一对工单冲突），未写入任何内容"},
    },
    summary="分代登记：旧件末卸工单 + 新件首装工单",
)
def register_generation(req: GenerationRequest, response: Response, conn=Depends(get_conn)):
    with _write_lock:
        try:
            result = core.apply_generation_registration(conn, req)
        except core.GenerationRejected as e:
            raise HTTPException(
                status_code=409,
                detail={"message": "分代登记被拒绝，未写入任何内容，查询保留原结果",
                        "errors": e.errors})
    response.status_code = 200 if result["idempotent"] else 201
    return GenerationInfo(**result["registration"])


@app.get(
    "/generations",
    response_model=list[GenerationInfo],
    summary="列出全部分代登记",
)
def list_generations(conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT part_no, old_end_order, new_start_order, old_end_ts, new_start_ts,"
        " created_at FROM part_generations ORDER BY part_no").fetchall()
    return [GenerationInfo(**dict(r)) for r in rows]


@app.get(
    "/generations/{part_no}",
    response_model=GenerationInfo,
    responses={404: {"description": "该件号未分代"}},
    summary="按件号查询分代登记",
)
def get_generation(part_no: str, conn=Depends(get_conn)):
    r = conn.execute(
        "SELECT part_no, old_end_order, new_start_order, old_end_ts, new_start_ts,"
        " created_at FROM part_generations WHERE part_no = ?", (part_no,)).fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail=f"件号 {part_no} 未分代")
    return GenerationInfo(**dict(r))


@app.get("/orders", response_model=list[OrderInfo], summary="列出当前有效工单（每号取最高修订号）")
def list_orders(conn=Depends(get_conn)):
    out = []
    for r in conn.execute(
            "SELECT order_no, revision, ts, car_no, mileage FROM work_orders"
            " ORDER BY ts, order_no"):
        chs = conn.execute(
            "SELECT position, off_part, on_part FROM order_changes WHERE order_no = ?"
            " ORDER BY position", (r["order_no"],)).fetchall()
        out.append(OrderInfo(
            order_no=r["order_no"], revision=r["revision"], ts=r["ts"],
            car_no=r["car_no"], mileage=r["mileage"],
            changes=[dict(c) for c in chs]))
    return out

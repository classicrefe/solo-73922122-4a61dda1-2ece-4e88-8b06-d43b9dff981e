"""FastAPI 入口：批量接收换装工单、按件号核算服役里程。"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response
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
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "检修数仓侧核算 API：批量接收换装工单，从空轴位重建换装历史，"
        "按件号累计服役里程（轴承换车后里程连续累计）。\n\n"
        "- 批次整批校验、整批提交、整批退回\n"
        "- 同号同版同内容重送幂等；旧版 / 同版异内容退回；更高修订号为更正\n"
        "- 同一时刻的换下换上整体判定，每轴位每刻最多变动一次\n"
        "- 查询只读最近完整提交的结果"
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
    responses={404: {"description": "件号无装车记录"}},
    summary="按件号查询装车段与累计服役里程",
)
def part_history(part_no: str, conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT * FROM segments WHERE part_no = ? ORDER BY start_ts, start_order",
        (part_no,)).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"件号 {part_no} 无装车记录")
    finished: list[FinishedSegment] = []
    active: ActiveSegment | None = None
    total = 0
    for r in rows:
        if r["end_order"] is None:
            active = ActiveSegment(car_no=r["car_no"], position=r["position"])
        else:
            delta = r["end_mileage"] - r["start_mileage"]
            total += delta
            finished.append(FinishedSegment(
                car_no=r["car_no"], position=r["position"],
                start_order=r["start_order"], start_ts=r["start_ts"],
                start_mileage=r["start_mileage"],
                end_order=r["end_order"], end_ts=r["end_ts"],
                end_mileage=r["end_mileage"], mileage_delta=delta))
    return PartHistory(part_no=part_no, total_mileage=total,
                       finished_segments=finished, active=active)


@app.get("/cars/{car_no}/state", response_model=CarState, summary="查询车辆当前轴位装件")
def car_state(car_no: str, conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT position, part_no, start_order, start_ts, start_mileage FROM segments"
        " WHERE car_no = ? AND end_order IS NULL ORDER BY position",
        (car_no,)).fetchall()
    return CarState(car_no=car_no, positions=[PositionState(
        position=r["position"], part_no=r["part_no"], since_order=r["start_order"],
        since_ts=r["start_ts"], since_mileage=r["start_mileage"]) for r in rows])


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

"""FastAPI application: dangerous-goods custody chain API."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import config, services, summary
from .database import connect, init_db
from .schemas import (
    BatchResponse,
    BatchSubmit,
    ChainResponse,
    RouteQuery,
    RouteSummaryResponse,
    SummaryStatusResponse,
    WaybillCreate,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    init_db()
    # Reuse the existing SQLite volume: build the route-prefix summary for
    # legacy waybills in the background.  New writes maintain it inline.
    summary.initialize_summary(config.DB_PATH)
    yield
    summary.stop_backfill()


app = FastAPI(
    title="空运危险品保管链 API",
    description=(
        "运单保管链（扫描交接 / 撤回 / 补报 / 更正）的修订裁决与可信前缀查询。"
        "同 ID 仅最高修订生效；同修订异文整批拒绝；失败保留旧链。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn() -> sqlite3.Connection:  # type: ignore[return]
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Error mapping: domain errors never leak 500s, and failed batches have
# already been rolled back by the service layer.
# --------------------------------------------------------------------------- #
@app.exception_handler(services.DomainConflict)
async def conflict_handler(_: Request, exc: services.DomainConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(services.DomainValidation)
async def not_found_handler(_: Request, exc: services.DomainValidation) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.exception_handler(summary.SummaryNotReady)
async def summary_not_ready_handler(_: Request, exc: summary.SummaryNotReady) -> JSONResponse:
    # 503: the legacy-volume backfill has not finished yet; queries are not
    # served until every committed waybill is reflected in the summary.
    return JSONResponse(
        status_code=503,
        content={
            "ready": False,
            "status": "BUILDING",
            "error": "summary_not_ready",
            "detail": str(exc),
        },
    )


@app.exception_handler(ValidationError)
async def pydantic_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "validation_error", "detail": exc.errors()})


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/api/waybills", status_code=201, tags=["waybills"])
def create_waybill(
    body: WaybillCreate, conn: sqlite3.Connection = Depends(get_conn)
) -> dict:
    """创建运单并固定其起始保管方。"""
    return services.create_waybill(conn, body)


@app.get("/api/waybills", tags=["waybills"])
def list_waybills(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    rows = conn.execute(
        "SELECT id, origin_party, created_at FROM waybills ORDER BY id"
    ).fetchall()
    return {"waybills": [dict(r) for r in rows]}


@app.post(
    "/api/waybills/{waybill_id}/scans/batch",
    response_model=BatchResponse,
    tags=["scans"],
)
def submit_batch(
    waybill_id: str,
    batch: BatchSubmit,
    conn: sqlite3.Connection = Depends(get_conn),
) -> BatchResponse:
    """批量提交交接/撤回。

    * HANDOVER：scan_id、revision、ts_utc（带时区）、giver、receiver
    * WITHDRAWAL：仅 scan_id、revision（必须高于已知最高修订）
    * 同修订异文、过期修订、撤回未知 ID -> 409，整批回滚，旧链保留
    """
    return services.apply_batch(conn, waybill_id, batch)


@app.get(
    "/api/waybills/{waybill_id}/chain",
    response_model=ChainResponse,
    tags=["scans"],
)
def query_chain(
    waybill_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> ChainResponse:
    """返回可信连续交接前缀、首处断链原因与当前可确认保管方。"""
    return services.compute_chain(conn, waybill_id)


# --------------------------------------------------------------------------- #
# Route-prefix summary queries (read the materialized summary only)
# --------------------------------------------------------------------------- #
@app.post(
    "/api/route-summary",
    response_model=RouteSummaryResponse,
    tags=["route-summary"],
)
def query_route_summary(
    query: RouteQuery, conn: sqlite3.Connection = Depends(get_conn)
) -> RouteSummaryResponse:
    """按路线前缀统计运单去向。

    输入从起始保管方开始的有序名单（允许重复保管方），仅统计可信保管链
    以该路线开头的运单；查询只读取已提交的持久化汇总（一次 GROUP BY），
    不逐单查链。补建未完成时返回 503（status=BUILDING）。
    """
    return summary.query_route_summary(conn, query.parties)


@app.get(
    "/api/route-summary/status",
    response_model=SummaryStatusResponse,
    tags=["route-summary"],
)
def route_summary_status(
    conn: sqlite3.Connection = Depends(get_conn),
) -> SummaryStatusResponse:
    """汇总补建进度：READY 可查；BUILDING 时返回待补建运单数。"""
    state = summary.get_status(conn)
    return SummaryStatusResponse(**state)


# --------------------------------------------------------------------------- #
# Minimal console (the interactive /docs remains fully functional too).
# --------------------------------------------------------------------------- #
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")

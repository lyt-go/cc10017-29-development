"""补数恢复清单接口：生成、筛选、冲突裁决、重试恢复与跨模块核对。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.backfill_recovery import DISPLAY_FIELDS, BackfillRecoveryService

router = APIRouter(prefix="/api/backfill_recovery", tags=["补数恢复清单"])

service = BackfillRecoveryService()

STATUSES = ["待恢复", "已完成"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受清单声明过的字段，忽略 page/size 等保留参数。"""
    accepted = {"清单编号", "任务编号", "目标表名", "来源批次"}
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in accepted and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按清单编号、任务编号、目标表或来源批次检索"),
    status: str | None = Query(default=None, description="待恢复、已完成"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """筛选恢复清单：排序按记录 id 稳定，分页不影响先后次序。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """按当前筛选条件导出 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *DISPLAY_FIELDS, "status", "剩余条数", "可合并"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in DISPLAY_FIELDS),
                  item.get("status", ""), item.get("剩余条数", 0), "是" if item.get("可合并") else "否"]
        row = []
        for value in values:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="backfill_recovery.csv"'})


@router.get("/check/{table_name}")
def check_table(table_name: str) -> dict:
    """合并清理调用：核对该表的恢复清单，给出可合并范围与阻塞原因。"""
    return service.check_table(table_name)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单份清单明细；与列表共用同一读模型，返回后字段保持一致。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"补数恢复清单 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def generate(payload: dict[str, Any] | None = None) -> ActionResult:
    """从补数数据任务生成恢复清单，已完成偏移与待补区间一起写入。"""
    created, message = service.generate(payload or {})
    if not created:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"items": created})


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对清单执行冲突裁决或重试恢复。"""
    action = payload.action.strip()
    remark = (payload.remark or "").strip()
    if action in ("续跑", "重放"):
        entry, message = service.resolve_conflict(entry_id, action)
    elif action in ("重试恢复", "重试补数"):
        entry, message = service.retry(entry_id)
    else:
        return ActionResult(ok=False, message=f"动作「{action}」不属于补数恢复清单可执行范围")
    if entry is None:
        return ActionResult(ok=False, message=message)
    if remark:
        entry["备注"] = remark
    return ActionResult(ok=True, message=message, entry=entry)

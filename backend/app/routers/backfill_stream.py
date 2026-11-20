"""流式补数 时间线接口：新增读取订单表补数数据任务并生成补数恢复数据清单。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.backfill_recovery import BackfillRecoveryService
from app.services.backfill_stream import BackfillStreamService

router = APIRouter(prefix="/api/backfill_stream", tags=["流式补数"])
service = BackfillStreamService()
recovery = BackfillRecoveryService()
LIST_FIELDS = ["任务编号", "目标表名", "起始偏移", "截止偏移", "重放速率", "并行度", "覆盖策略", "结果校验"]
STATUSES = ["草稿", "运行中", "已中断", "已完成", "已取消"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("")
def list_entries() -> dict[str, Any]:
    items = service.get_timeline()
    return {"items": items}


@router.get("/orders/tasks")
def list_order_backfill_tasks(
    status: str | None = Query(default=None, description="草稿、运行中、已中断、已完成、已取消"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """读取订单表补数数据任务：带出已完成偏移与待补区间，供生成恢复清单。"""
    filters = {"目标表名": "orders"}
    items, total = service.list_entries(filters=filters, status=status, page=page, size=size)
    progress = [recovery.find_by_task(str(item.get("任务编号"))) for item in items]
    rows = []
    for item, manifest in zip(items, progress):
        item = dict(item)
        item["恢复清单"] = manifest.get("清单编号") if manifest else ""
        item["恢复清单状态"] = manifest.get("status") if manifest else "未生成"
        rows.append(item)
    return {"items": rows, "total": total, "page": page, "size": size}


@router.post("/orders/recovery")
def generate_order_recovery() -> ActionResult:
    """读取订单表补数数据任务并生成补数恢复数据清单。"""
    created, message = recovery.generate({"目标表名": "orders"})
    if not created:
        return ActionResult(ok=False, message=message)
    tasks = [
        {"任务编号": item.get("任务编号"), "已完成偏移": item.get("已完成偏移"),
         "待补区间": item.get("待补区间"), "来源批次": item.get("来源批次")}
        for item in created
    ]
    return ActionResult(ok=True, message=message, entry={"manifest": created, "tasks": tasks})


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.get_timeline()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=backfill_stream.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"补数任务 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="补数任务已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)

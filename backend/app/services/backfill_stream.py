"""流式补数 时间线服务：生成作业时间序列和示例数据。

补数任务中断时把已完成偏移、检查点偏移与待补区间一起落到任务上，
补数恢复清单直接读取这些字段生成，恢复时不再从 0 重放。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.store import store

MODULE = "backfill_stream"
REQUIRED_FIELDS = ["任务编号", "目标表名", "起始偏移"]
STATUS_ORDER = ["草稿", "运行中", "已中断", "已完成", "已取消"]
ACTION_RULES = {"启动补数": "运行中", "中断补数": "已中断", "确认完成": "已完成", "取消补数": "已取消"}
NEGATIVE_ACTIONS = ["中断补数"]


def parse_offset(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class BackfillStreamService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = sorted(store.rows(MODULE), key=lambda row: int(row.get("id", 0)))
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("任务编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_timeline(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        today = date.today()
        items: list[dict] = []
        for i, row in enumerate(rows):
            start = (today - timedelta(days=7 - i * 2)).isoformat()
            end = (today + timedelta(days=3 - i)).isoformat()
            completed = parse_offset(row.get("已完成偏移"), parse_offset(row.get("起始偏移")))
            items.append({
                "id": int(row.get("id", i + 1)),
                "name": str(row.get("任务编号", "")),
                "start": start,
                "end": end,
                "status": str(row.get("status", STATUS_ORDER[i % len(STATUS_ORDER)])),
                "目标表名": row.get("目标表名", ""),
                "已完成偏移": completed,
                "待补区间": str(row.get("待补区间") or ""),
                "来源批次": str(row.get("来源批次") or ""),
                "logs": (
                    f"[{start}] 作业启动\n[{start}] 正在处理...\n"
                    f"[{end}] 已完成偏移 {completed}，待补区间 {row.get('待补区间') or '无'}"
                ),
            })
        return items

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        for f in ["截止偏移", "重放速率", "并行度", "覆盖策略", "结果校验", "来源批次"]:
            entry[f] = values.get(f, "")
        completed = parse_offset(values.get("起始偏移"))
        end = parse_offset(values.get("截止偏移"), completed)
        entry["已完成偏移"] = completed
        entry["检查点偏移"] = completed
        entry["待补区间"] = f"{completed}-{end}" if end > completed else ""
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"补数任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流式补数可执行范围"
        target = ACTION_RULES[action]

        if action == "中断补数":
            self._record_interrupt(entry)
        if action == "确认完成":
            entry["已完成偏移"] = parse_offset(entry.get("截止偏移"), parse_offset(entry.get("已完成偏移")))
            entry["待补区间"] = ""

        entry["status"] = target
        entry["pending"] = target not in (STATUS_ORDER[-2], STATUS_ORDER[-1])
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"补数任务已{action}"

    def _record_interrupt(self, entry: dict[str, Any]) -> None:
        """中断时保留处理进度：已完成偏移、检查点偏移与剩余区间一起写回任务。"""
        completed = parse_offset(entry.get("已完成偏移"), parse_offset(entry.get("起始偏移")))
        end = parse_offset(entry.get("截止偏移"), completed)
        entry["检查点偏移"] = completed
        entry["已完成偏移"] = completed
        entry["待补区间"] = f"{completed}-{end}" if end > completed else ""

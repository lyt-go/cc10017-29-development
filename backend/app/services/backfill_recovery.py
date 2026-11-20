"""补数恢复清单业务规则。

清单围绕「已完成偏移 + 待补区间」成对落账：
- 生成清单时从补数任务读取已完成偏移与截止偏移，一次写入两者；
- 重试恢复只应用待补区间，已完成偏移以内的记录按边界跳过，避免重复批次；
- 已完成偏移与迟到记录在重启点相遇时先做冲突裁决，清单只声明一个恢复起点；
- 合并清理通过 check_table 读取这份清单，待补区间未完成时不放行合并。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "backfill_recovery"
LIST_FIELDS = ["清单编号", "任务编号", "目标表名", "来源批次"]
DISPLAY_FIELDS = [
    "清单编号", "任务编号", "目标表名", "来源批次", "已完成偏移", "待补区间",
    "恢复起点", "恢复策略", "检查点偏移", "重放区间", "冲突判定", "数据版本",
]
STATUS_ORDER = ["待恢复", "已完成"]
STRATEGIES = ["续跑", "重放"]
CONFLICT_NONE = "无冲突"
CONFLICT_PENDING = "待裁决"
CONFLICT_RESOLVED = "已裁决"


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_offset(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_range(text: Any) -> tuple[int, int] | None:
    """解析「起点-终点」形式的待补区间；无法解析时返回 None。"""
    source = str(text or "").strip()
    if not source or "-" not in source:
        return None
    start_text, end_text = source.split("-", 1)
    try:
        start, end = int(start_text), int(end_text)
    except ValueError:
        return None
    return (start, end) if end >= start else None


def format_range(start: int, end: int) -> str:
    return f"{start}-{end}" if end > start else ""


class BackfillRecoveryService:
    # ───────────────────────────────
    # 查询
    # ───────────────────────────────

    def public_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表与详情共用同一份读模型，保证两个入口返回后字段一致。"""
        item = dict(row)
        pending = parse_range(row.get("待补区间"))
        item["待补区间起点"] = pending[0] if pending else None
        item["待补区间终点"] = pending[1] if pending else None
        item["剩余条数"] = (pending[1] - pending[0]) if pending else 0
        item["可合并"] = not pending and row.get("冲突判定") != CONFLICT_PENDING
        item["待补区间"] = str(row.get("待补区间") or "")
        item["来源批次"] = str(row.get("来源批次") or "")
        item["恢复策略"] = str(row.get("恢复策略") or "")
        item["冲突判定"] = str(row.get("冲突判定") or CONFLICT_NONE)
        item["数据版本"] = parse_offset(row.get("数据版本"), 1)
        item["已完成偏移"] = parse_offset(row.get("已完成偏移"))
        return item

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
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if any(needle in str(row.get(field, "")).casefold()
                       for field in ("清单编号", "任务编号", "目标表名", "来源批次"))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return [self.public_row(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self.public_row(row) if row else None

    def find_by_task(self, task_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("任务编号")) == str(task_code):
                return row
        return None

    def check_table(self, target_table: str) -> dict[str, Any]:
        """给合并清理模块核对可合并范围：待补区间与未裁决冲突都会阻塞合并。"""
        rows = sorted(
            (row for row in store.rows(MODULE) if str(row.get("目标表名")) == str(target_table)),
            key=lambda row: int(row.get("id", 0)),
        )
        manifests = [self.public_row(row) for row in rows]
        pending = [item for item in manifests if item["剩余条数"] > 0]
        conflicts = [item for item in manifests if item["冲突判定"] == CONFLICT_PENDING]
        source_batches = sorted({item["来源批次"] for item in manifests if item["来源批次"]})
        mergeable_ranges = [
            f"0-{item['已完成偏移']}" for item in manifests
            if item["剩余条数"] == 0 and item["已完成偏移"] > 0
        ]
        reasons: list[str] = []
        if pending:
            reasons.append("仍有待补区间：" + "、".join(
                f"{item['清单编号']} {item['待补区间']}" for item in pending))
        if conflicts:
            reasons.append("存在未裁决的重启点冲突：" + "、".join(
                item["清单编号"] for item in conflicts))
        return {
            "目标表名": target_table,
            "can_merge": not reasons,
            "reasons": reasons,
            "pending": pending,
            "conflicts": conflicts,
            "source_batches": source_batches,
            "mergeable_ranges": mergeable_ranges,
            "manifests": manifests,
        }

    # ───────────────────────────────
    # 生成清单
    # ───────────────────────────────

    def generate(self, values: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], str]:
        """读取补数数据任务并生成恢复清单；同一任务只保留一份未完成清单。"""
        values = values or {}
        task_code = str(values.get("任务编号") or "").strip()
        target_table = str(values.get("目标表名") or "").strip()
        tasks = [row for row in store.rows("backfill_stream")
                 if str(row.get("status")) != "已取消"]
        if task_code:
            tasks = [row for row in tasks if str(row.get("任务编号")) == task_code]
        elif target_table:
            tasks = [row for row in tasks if str(row.get("目标表名")) == target_table]
        else:
            tasks = [row for row in tasks if str(row.get("目标表名")) == "orders"]

        if not tasks:
            detail = task_code or target_table or "orders"
            return [], f"没有可生成恢复清单的补数任务：{detail}"

        created: list[dict[str, Any]] = []
        for task in tasks:
            item, _ = self._upsert_from_task(task)
            created.append(item)
        return created, f"已生成 {len(created)} 份补数恢复清单"

    def _upsert_from_task(self, task: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        completed = parse_offset(task.get("已完成偏移"), parse_offset(task.get("起始偏移")))
        end = parse_offset(task.get("截止偏移"), completed)
        checkpoint = parse_offset(task.get("检查点偏移"), completed)
        pending = format_range(completed, end)
        batch = str(task.get("来源批次") or f"BATCH-{str(task.get('目标表名', 'table')).upper()}-{int(task.get('id', 0)):04d}")

        existing = self.find_by_task(str(task.get("任务编号")))
        if existing is not None and existing.get("status") == "已完成" and not pending:
            # 已完成的任务重复生成清单时保持原清单，不新开记录也不改版本。
            return self.public_row(existing), False
        if existing is not None:
            # 同一任务的中断重试，只刷新进度，不新开清单、也不凭空加版本。
            existing["目标表名"] = task.get("目标表名")
            existing["来源批次"] = batch
            existing["已完成偏移"] = completed
            existing["检查点偏移"] = checkpoint
            existing["待补区间"] = pending
            if not pending:
                existing["status"] = "已完成"
                existing["pending"] = False
            elif existing.get("status") == "已完成":
                # 任务重新开了区间：清单跟着回到待恢复，进度以任务为准。
                existing["status"] = "待恢复"
                existing["pending"] = True
            if existing.get("冲突判定") != CONFLICT_RESOLVED:
                existing["冲突判定"] = self.detect_conflict(
                    str(task.get("任务编号")), completed, checkpoint)
            return self.public_row(existing), False

        rows = store.rows(MODULE)
        entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        entry = {
            "id": entry_id,
            "status": "待恢复" if pending else "已完成",
            "pending": bool(pending),
            "abnormal": False,
            "清单编号": f"RECV-{entry_id:04d}",
            "任务编号": task.get("任务编号"),
            "目标表名": task.get("目标表名"),
            "来源批次": batch,
            "已完成偏移": completed,
            "待补区间": pending,
            "恢复起点": completed,
            "恢复策略": "续跑",
            "检查点偏移": checkpoint,
            "重放区间": "",
            "冲突判定": self.detect_conflict(str(task.get("任务编号")), completed, checkpoint),
            "数据版本": 1,
            "更新时间": now_text(),
            "version_history": [],
        }
        entry["version_history"].append({
            "版本": 1, "变更": "生成清单", "恢复起点": completed, "恢复策略": "续跑",
            "已完成偏移": completed, "待补区间": pending, "来源批次": batch,
            "时间": entry["更新时间"],
        })
        rows.append(entry)
        return self.public_row(entry), True

    def detect_conflict(self, task_code: str, completed: int, checkpoint: int) -> str:
        """重启点相遇：迟到记录落在 (检查点, 已完成偏移] 区间时两种恢复策略有歧义。"""
        for row in store.rows("backfill_late_records"):
            if str(row.get("任务编号")) != str(task_code):
                continue
            offset = parse_offset(row.get("偏移"), -1)
            if checkpoint < offset <= completed:
                return CONFLICT_PENDING
        return CONFLICT_NONE

    def conflict_offsets(self, task_code: str, completed: int, checkpoint: int) -> list[int]:
        offsets = []
        for row in store.rows("backfill_late_records"):
            if str(row.get("任务编号")) != str(task_code):
                continue
            offset = parse_offset(row.get("偏移"), -1)
            if checkpoint < offset <= completed:
                offsets.append(offset)
        return sorted(offsets)

    # ───────────────────────────────
    # 冲突裁决与重试
    # ───────────────────────────────

    def resolve_conflict(self, entry_id: int, strategy: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"补数恢复清单 {entry_id} 不存在或已归档"
        if strategy not in STRATEGIES:
            return None, f"恢复策略「{strategy}」不在允许范围（{'、'.join(STRATEGIES)}）"
        if entry.get("冲突判定") == CONFLICT_NONE:
            return None, "清单不存在重启点冲突，无需裁决"
        completed = parse_offset(entry.get("已完成偏移"))
        checkpoint = parse_offset(entry.get("检查点偏移"), completed)
        start = completed if strategy == "续跑" else checkpoint
        replay_range = "" if strategy == "续跑" else format_range(start, completed)
        self._apply_version(entry, {
            "变更": f"冲突裁决-{strategy}",
            "恢复起点": start,
            "恢复策略": strategy,
            "重放区间": replay_range,
            "冲突判定": CONFLICT_RESOLVED,
        })
        return self.public_row(entry), (
            f"清单 {entry.get('清单编号')} 已按「{strategy}」裁决，唯一恢复起点为 {start}")

    def retry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"补数恢复清单 {entry_id} 不存在或已归档"
        if entry.get("冲突判定") == CONFLICT_PENDING:
            return None, "已完成偏移与迟到记录在重启点相遇，请先裁决恢复起点（续跑或重放）"
        pending = parse_range(entry.get("待补区间"))
        if pending is None:
            return None, f"清单 {entry.get('清单编号')} 没有待补区间，无需重试"

        completed = parse_offset(entry.get("已完成偏移"))
        start = parse_offset(entry.get("恢复起点"), completed)
        strategy = str(entry.get("恢复策略") or "续跑")
        end = pending[1]
        task_code = str(entry.get("任务编号"))

        # 去重边界来自清单的已完成偏移，而不是只看任务状态：
        # 重放策略下 [恢复起点, 已完成偏移] 已写过，必须按边界跳过。
        duplicate_skipped = max(0, completed - start) if strategy == "重放" else 0
        if strategy == "重放":
            late_skipped = sum(
                1 for row in store.rows("backfill_late_records")
                if str(row.get("任务编号")) == task_code
                and parse_offset(row.get("偏移"), -1) < start
            )
        else:
            late_skipped = sum(
                1 for row in store.rows("backfill_late_records")
                if str(row.get("任务编号")) == task_code
                and parse_offset(row.get("偏移"), -1) < completed
            )
        applied = max(0, end - max(start, pending[0]))

        self._apply_version(entry, {
            "变更": "重试恢复",
            "恢复起点": start,
            "恢复策略": strategy,
            "已完成偏移": end,
            "待补区间": "",
            "status": "已完成",
            "pending": False,
        })
        entry["重试结果"] = {
            "应用条数": applied,
            "跳过重复条数": duplicate_skipped,
            "跳过迟到条数": late_skipped,
            "恢复起点": start,
            "数据版本": entry["数据版本"],
            "来源批次": entry.get("来源批次", ""),
        }

        task = self._find_task(task_code)
        if task is not None:
            task["已完成偏移"] = end
            task["待补区间"] = ""
            task["status"] = "已完成"
            task["pending"] = False

        return self.public_row(entry), (
            f"清单 {entry.get('清单编号')} 已补齐后续项：应用 {applied} 条，"
            f"按已完成偏移跳过重复 {duplicate_skipped} 条")

    def _apply_version(self, entry: dict[str, Any], changes: dict[str, Any]) -> None:
        version = parse_offset(entry.get("数据版本"), 1) + 1
        entry["数据版本"] = version
        entry["更新时间"] = now_text()
        for key, value in changes.items():
            if key == "变更":
                continue
            entry[key] = value
        if entry.get("status") == "已完成":
            entry["pending"] = False
        history = entry.setdefault("version_history", [])
        history.append({
            "版本": version,
            "变更": changes.get("变更", ""),
            "恢复起点": entry.get("恢复起点"),
            "恢复策略": entry.get("恢复策略", ""),
            "已完成偏移": entry.get("已完成偏移"),
            "待补区间": entry.get("待补区间", ""),
            "来源批次": entry.get("来源批次", ""),
            "时间": entry["更新时间"],
        })

    def _find_task(self, task_code: str) -> dict[str, Any] | None:
        for row in store.rows("backfill_stream"):
            if str(row.get("任务编号")) == task_code:
                return row
        return None

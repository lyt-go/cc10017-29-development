"""合并清理业务规则：状态流转、字段校验与筛选口径都收在这里。

合并前必须核对补数恢复清单：待补区间未完成或重启点冲突未裁决时不允许合并订单文件，
可合并时把来源批次随合并产物一起交接，避免跨模块丢批次。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.backfill_recovery import BackfillRecoveryService
from app.store import store

MODULE = "compact_policy"
LIST_FIELDS = ["策略编号", "目标表名", "触发条件", "合并并发", "目标文件大小", "最小文件数", "保留快照数", "执行时间窗口"]
REQUIRED_FIELDS = ["策略编号", "目标表名", "触发条件"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {
    "提交校验": "待校验", "标记生效": "已生效", "停用策略": "已停用",
    "合并文件": "已生效",
}
NEGATIVE_ACTIONS = ["停用策略"]


class CompactPolicyService:
    def __init__(self) -> None:
        self.recovery = BackfillRecoveryService()

    def recovery_check(self, target_table: str) -> dict[str, Any]:
        """合并清理页核对入口：复用补数恢复清单的可合并范围判定。"""
        return self.recovery.check_table(target_table)

    def merge_files(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """等待待补区间完成后再合并订单文件，合并产物保留来源批次。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清理策略 {entry_id} 不存在或已归档"
        target_table = str(entry.get("目标表名") or "")
        check = self.recovery_check(target_table)
        if not check["can_merge"]:
            return None, "补数恢复清单未放行：" + "；".join(check["reasons"])
        batches = sorted({
            item["来源批次"] for item in check["manifests"]
            if item.get("来源批次") and item.get("可合并")
        })

        artifact = {
            "compact_policy_id": entry_id,
            "target_table": target_table,
            "source_batches": batches,
            "covered_ranges": check["mergeable_ranges"],
            "merged_files": len(batches) + 1,
            "compacted_at": datetime.now().isoformat(timespec="seconds"),
        }
        store.rows("compact_results").append(artifact)
        entry["last_merge"] = artifact
        return entry, f"表 {target_table} 已合并，保留来源批次 {'、'.join(batches)}"

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("策略编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清理策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于合并清理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "合并文件":
            merged, message = self.merge_files(entry_id)
            if merged is None:
                return None, message
            entry["pending"] = False
            return entry, message
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"清理策略已{action}"

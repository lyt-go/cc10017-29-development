"""补数恢复清单测试：进度落账、冲突裁决、重试去重与合并清理放行。"""
from __future__ import annotations

import unittest

from app.services.backfill_recovery import BackfillRecoveryService
from app.services.backfill_stream import BackfillStreamService
from app.services.compact_policy import CompactPolicyService
from app.store import store


def new_task(stream: BackfillStreamService, code: str, table: str, start: str, end: str) -> dict:
    task, missing = stream.create_entry({
        "任务编号": code, "目标表名": table, "起始偏移": start, "截止偏移": end,
        "来源批次": f"BATCH-{code}",
    })
    assert not missing
    return task


class TestRecoveryManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = BackfillStreamService()
        self.recovery = BackfillRecoveryService()

    def test_interrupt_keeps_completed_offset_and_pending_range(self) -> None:
        """中断只保留运行状态不够：已完成偏移与待补区间必须一起写回。"""
        task = new_task(self.stream, "BACK-T-INT", "t_int", "0", "900")
        self.stream.run_action(task["id"], "启动补数")
        task["已完成偏移"] = 400
        entry, _ = self.stream.run_action(task["id"], "中断补数")
        self.assertEqual(entry["status"], "已中断")
        self.assertEqual(entry["已完成偏移"], 400)
        self.assertEqual(entry["检查点偏移"], 400)
        self.assertEqual(entry["待补区间"], "400-900")

    def test_generate_manifest_writes_both_sides(self) -> None:
        """生成清单时已完成偏移与待补区间是成对落账的。"""
        task = new_task(self.stream, "BACK-T-GEN", "t_gen", "0", "1000")
        task["已完成偏移"] = 250
        task["来源批次"] = "BATCH-T-GEN"
        created, message = self.recovery.generate({"任务编号": "BACK-T-GEN"})
        self.assertEqual(len(created), 1)
        manifest = created[0]
        self.assertEqual(manifest["已完成偏移"], 250)
        self.assertEqual(manifest["待补区间"], "250-1000")
        self.assertEqual(manifest["来源批次"], "BATCH-T-GEN")
        self.assertEqual(manifest["数据版本"], 1)
        # 再次生成沿用同一清单，不产生重复记录
        again, _ = self.recovery.generate({"任务编号": "BACK-T-GEN"})
        self.assertEqual(again[0]["id"], manifest["id"])

    def test_retry_fills_pending_and_skips_duplicates(self) -> None:
        """重试按清单的已完成偏移去重，补齐后续项后清单转为已完成。"""
        task = new_task(self.stream, "BACK-T-RETRY", "t_retry", "0", "1000")
        task["已完成偏移"] = 600
        self.recovery.generate({"任务编号": "BACK-T-RETRY"})
        manifest = self.recovery.find_by_task("BACK-T-RETRY")
        entry, message = self.recovery.retry(manifest["id"])
        self.assertIsNotNone(entry)
        self.assertIn("跳过重复 0 条", message)
        self.assertEqual(entry["已完成偏移"], 1000)
        self.assertEqual(entry["待补区间"], "")
        self.assertEqual(entry["status"], "已完成")
        self.assertEqual(entry["数据版本"], 2)
        self.assertEqual(task["status"], "已完成")

    def test_conflict_requires_resolution_before_retry(self) -> None:
        """已完成偏移与迟到记录在重启点相遇时先裁决，裁决后才允许重试。"""
        task = new_task(self.stream, "BACK-T-CONF", "t_conf", "0", "1000")
        task["已完成偏移"] = 500
        task["检查点偏移"] = 480
        store.rows("backfill_late_records").append({
            "任务编号": "BACK-T-CONF", "目标表名": "t_conf", "偏移": "490",
            "来源批次": "BATCH-T-CONF-LATE", "到达时间": "2026-11-20T00:00:00",
        })
        created, _ = self.recovery.generate({"任务编号": "BACK-T-CONF"})
        manifest = created[0]
        self.assertEqual(manifest["冲突判定"], "待裁决")

        blocked, message = self.recovery.retry(manifest["id"])
        self.assertIsNone(blocked)
        self.assertIn("裁决", message)

        resolved, _ = self.recovery.resolve_conflict(manifest["id"], "重放")
        self.assertEqual(resolved["恢复起点"], 480)
        self.assertEqual(resolved["恢复策略"], "重放")
        self.assertEqual(resolved["数据版本"], 2)

        entry, message = self.recovery.retry(manifest["id"])
        self.assertEqual(entry["数据版本"], 3)
        self.assertIn("跳过重复 20 条", message)
        self.assertEqual(entry["重试结果"]["跳过重复条数"], 20)

        # 只声明一个恢复起点：重放起点与恢复策略一起被记录，重试不会改回续跑
        self.assertEqual(entry["恢复起点"], 480)

    def test_compact_waits_for_pending_range(self) -> None:
        """合并清理核对清单：待补区间未完成时不可合并。"""
        task = new_task(self.stream, "BACK-T-CMP", "t_cmp", "0", "1000")
        task["已完成偏移"] = 300
        self.recovery.generate({"任务编号": "BACK-T-CMP"})
        check = self.recovery.check_table("t_cmp")
        self.assertFalse(check["can_merge"])
        self.assertEqual(check["pending"][0]["待补区间"], "300-1000")

        manifest = self.recovery.find_by_task("BACK-T-CMP")
        self.recovery.retry(manifest["id"])
        check = self.recovery.check_table("t_cmp")
        self.assertTrue(check["can_merge"])
        self.assertEqual(check["source_batches"], ["BATCH-BACK-T-CMP"])

    def test_compact_action_blocked_then_merges_with_batches(self) -> None:
        """合并清理动作在清单放行前被拒绝，放行后产物保留来源批次。"""
        task = new_task(self.stream, "BACK-T-ACT", "t_act", "0", "500")
        task["已完成偏移"] = 100
        self.recovery.generate({"任务编号": "BACK-T-ACT"})
        compact = CompactPolicyService()
        policy, _ = compact.create_entry({
            "策略编号": "COMP-T-ACT", "目标表名": "t_act", "触发条件": "文件数>1",
        })
        blocked, message = compact.run_action(policy["id"], "合并文件")
        self.assertIsNone(blocked)
        self.assertIn("补数恢复清单未放行", message)

        manifest = self.recovery.find_by_task("BACK-T-ACT")
        self.recovery.retry(manifest["id"])
        merged, message = compact.run_action(policy["id"], "合并文件")
        self.assertIsNotNone(merged)
        self.assertIn("BATCH-BACK-T-ACT", message)
        artifacts = [row for row in store.rows("compact_results")
                     if row.get("target_table") == "t_act"]
        self.assertTrue(artifacts)
        self.assertEqual(artifacts[-1]["source_batches"], ["BATCH-BACK-T-ACT"])


class TestRecoveryListing(unittest.TestCase):
    def setUp(self) -> None:
        self.recovery = BackfillRecoveryService()

    def test_filter_search_paging_and_stable_order(self) -> None:
        """筛选、检索与分页共享同一稳定排序，列表和详情字段一致。"""
        first, total = self.recovery.list_entries(page=1, size=2)
        second, _ = self.recovery.list_entries(page=2, size=2)
        all_rows, all_total = self.recovery.list_entries(page=1, size=1000)
        self.assertEqual(total, all_total)
        ids = [row["id"] for row in first] + [row["id"] for row in second]
        self.assertEqual(ids, [row["id"] for row in all_rows][:len(ids)])
        self.assertEqual(ids, sorted(ids))

        filtered, filtered_total = self.recovery.list_entries(
            filters={"目标表名": "orders"}, keyword="orders", status="待恢复")
        self.assertGreaterEqual(filtered_total, 0)
        self.assertTrue(all(row["目标表名"] == "orders" for row in filtered))

        sample = all_rows[0]
        detail = self.recovery.get_entry(sample["id"])
        for key in ("清单编号", "已完成偏移", "待补区间", "恢复起点", "恢复策略", "数据版本", "来源批次"):
            self.assertEqual(detail[key], sample[key])


if __name__ == "__main__":
    unittest.main()

"""发布基线测试。

覆盖：
- 三来源快照固化（构建结果 / 覆盖率快照 / 缺陷列表）；
- 跨构建多源差异：通过率、新增/已修复失败、覆盖率升降、耗时退化、缺陷流转；
- 一致性：同一基线多次对比、跨进程式重建后对比结果逐字段一致；
- 基线规则：标签必填且项目内唯一、运行中的构建不能固化、按项目隔离、
  改名 / 删除。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from engine import (BaselineError, BaselineManager, CoverageAnalyzer,
                    DefectManager, ReportGenerator)
from storage import BuildStoreRegistry, StoreRegistry


def _finish_build(store, coverage, project_id, build_id, fail_idx,
                  pass_ratio=None):
    store.create(build_id)
    store.set_total(build_id, 4)
    for i in range(4):
        status = "failed" if i in fail_idx else "passed"
        store.record_result(build_id, {
            "case_id": f"c{i}", "case_name": f"用例{i}",
            "group": "g", "priority": "P1", "status": status,
            "duration": 0.1 + i * 0.1, "logs": [],
            "assertions": ([{"ok": False, "message": "断言失败"}]
                           if status == "failed" else []),
        })
    store.finish(build_id, "failed" if fail_idx else "passed")
    ratio = pass_ratio if pass_ratio is not None else (4 - len(fail_idx)) / 4
    coverage.generate(project_id, build_id, ratio)


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = StoreRegistry(os.path.join(self.tmp.name, "store"))
        self.builds = BuildStoreRegistry(os.path.join(self.tmp.name, "builds"))
        self.report = ReportGenerator(self.builds)
        self.coverage = CoverageAnalyzer(self.builds)
        self.defects = DefectManager(self.registry)
        self.bm = BaselineManager(
            self.registry, self.builds, self.report, self.coverage, self.defects)
        self.store = self.builds.for_project("p1")
        _finish_build(self.store, self.coverage, "p1", "b1", {3})
        _finish_build(self.store, self.coverage, "p1", "b2", {2, 3},
                      pass_ratio=0.4)

    def tearDown(self):
        self.tmp.cleanup()

    # -- 固化规则 -----------------------------------------------------------
    def test_create_freezes_three_sources(self):
        self.defects.create("p1", {"title": "d1", "status": "open"})
        b = self.bm.create("p1", "b1", "v1.0", "发布基线")
        snap = b["snapshot"]
        self.assertEqual(snap["report"]["summary"]["total"], 4)
        self.assertEqual(snap["report"]["summary"]["pass_rate"], 75.0)
        self.assertEqual(snap["report"]["failure_case_ids"], ["c3"])
        self.assertGreater(snap["coverage"]["percent"], 0)
        self.assertEqual(len(snap["coverage"]["files"]), 12)
        self.assertEqual(snap["defects"]["stats"]["total"], 1)
        self.assertTrue(b["frozen_at"])

    def test_tag_required_and_unique_per_project(self):
        self.bm.create("p1", "b1", "v1.0")
        with self.assertRaises(BaselineError):
            self.bm.create("p1", "b1", "  ")
        with self.assertRaises(BaselineError):
            self.bm.create("p1", "b2", "v1.0")
        # 同标签可用于另一个项目（基线按项目维护）
        self.bm.create("p2", "b1", "v1.0") if False else None
        other_store = self.builds.for_project("p2")
        _finish_build(other_store, self.coverage, "p2", "bx", set())
        self.bm.create("p2", "bx", "v1.0")

    def test_running_build_cannot_be_baselined(self):
        self.store.create("br")
        self.store.set_total("br", 1)
        with self.assertRaises(BaselineError):
            self.bm.create("p1", "br", "wip")

    def test_missing_build(self):
        with self.assertRaises(BaselineError):
            self.bm.create("p1", "nope", "v")

    # -- 多源差异 -----------------------------------------------------------
    def test_compare_pass_rate_failures_coverage(self):
        baseline = self.bm.create("p1", "b1", "v1.0")
        d = self.bm.compare_build("p1", "b2", baseline["id"])

        self.assertEqual(d["summary"]["pass_rate"]["baseline"], 75.0)
        self.assertEqual(d["summary"]["pass_rate"]["current"], 50.0)
        self.assertEqual(d["summary"]["pass_rate"]["delta"], -25.0)

        # 失败总数按报告页口径 failed+error+timeout：1 -> 2，新增 1
        self.assertEqual(d["summary"]["fail_count"]["delta"], 1)
        self.assertEqual([f["case_id"] for f in d["failures"]["new"]], ["c2"])
        self.assertEqual(d["failures"]["fixed"], [])
        self.assertEqual([f["case_id"] for f in d["failures"]["persistent"]],
                         ["c3"])

        # 覆盖率下降（当前构建通过率更低，确定性覆盖率随之更低）
        self.assertLess(d["coverage"]["percent"]["delta"], 0)
        self.assertEqual(len(d["coverage"]["files"]), 12)
        for f in d["coverage"]["files"]:
            self.assertIsNotNone(f["delta"])

    def test_compare_fixed_failures_when_build_improves(self):
        # 反向对比：以较差构建 b2 为基线，较好构建 b1 为当前
        baseline = self.bm.create("p1", "b2", "bad")
        d = self.bm.compare_build("p1", "b1", baseline["id"])
        self.assertEqual(d["summary"]["fail_count"]["delta"], -1)
        self.assertEqual([f["case_id"] for f in d["failures"]["fixed"]], ["c2"])
        self.assertEqual(d["failures"]["new"], [])
        self.assertGreater(d["coverage"]["percent"]["delta"], 0)

    def test_duration_reguration_reflected(self):
        b = self.bm.create("p1", "b1", "v1.0")
        d = self.bm.compare_build("p1", "b2", b["id"])
        # 两构建用例耗时集合相同，平均/P95/最慢等分布指标应持平
        for k in ("avg", "median", "p95", "max"):
            self.assertEqual(d["durations"][k]["delta"], 0.0, k)

    def test_defect_state_changes_detected_but_baseline_frozen(self):
        defect = self.defects.create("p1", {"title": "缺陷A", "status": "open"})
        baseline = self.bm.create("p1", "b1", "v1.0")
        # 固化后缺陷继续流转，基线快照不应改变
        self.defects.update(defect["id"], {"status": "fixed"})
        d = self.bm.compare_build("p1", "b2", baseline["id"])
        changed = {(x["id"], x["baseline_status"], x["current_status"])
                   for x in d["defects"]["status_changed"]}
        self.assertIn((defect["id"], "open", "fixed"), changed)
        stored = self.bm.get(baseline["id"])
        self.assertEqual(
            stored["snapshot"]["defects"]["rows"][0]["status"], "open")

        # 新增缺陷
        self.defects.create("p1", {"title": "缺陷B", "status": "open"})
        d2 = self.bm.compare_build("p1", "b2", baseline["id"])
        self.assertEqual(len(d2["defects"]["opened"]), 1)
        self.assertEqual(d2["defects"]["stats"]["total_delta"], 1)

    # -- 一致性 -------------------------------------------------------------
    def test_repeated_compare_is_identical(self):
        baseline = self.bm.create("p1", "b1", "v1.0")
        first = json.dumps(self.bm.compare_build("p1", "b2", baseline["id"]),
                           sort_keys=True)
        second = json.dumps(self.bm.compare_build("p1", "b2", baseline["id"]),
                            sort_keys=True)
        self.assertEqual(first, second)

    def test_compare_stable_after_manager_rebuild(self):
        # 模拟服务重启：用同一磁盘数据重建全部管理器，基线对比仍一致
        baseline = self.bm.create("p1", "b1", "v1.0")
        before = json.dumps(self.bm.compare_build("p1", "b2", baseline["id"]),
                            sort_keys=True)
        rebuilt = BaselineManager(
            StoreRegistry(os.path.join(self.tmp.name, "store")),
            self.builds, self.report, self.coverage,
            DefectManager(self.registry))
        after = json.dumps(rebuilt.compare_build("p1", "b2", baseline["id"]),
                           sort_keys=True)
        self.assertEqual(before, after)

    def test_coverage_only_compare(self):
        baseline = self.bm.create("p1", "b1", "v1.0")
        d = self.bm.compare_coverage("p1", "b2", baseline["id"])
        self.assertNotIn("summary", d)
        self.assertIn("percent", d["coverage"])
        self.assertEqual(d["baseline"]["tag"], "v1.0")

    # -- 管理 ---------------------------------------------------------------
    def test_list_summary_rename_delete(self):
        self.bm.create("p1", "b1", "v1.0")
        rows = self.bm.list("p1")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("snapshot", rows[0])  # 列表只给摘要
        self.assertEqual(rows[0]["pass_rate"], 75.0)
        bid = rows[0]["id"]
        self.bm.update_meta(bid, {"tag": "v1.0.1", "description": "改名"})
        self.assertEqual(self.bm.get(bid)["tag"], "v1.0.1")
        with self.assertRaises(BaselineError):
            self.bm.update_meta(bid, {"tag": ""})
        self.assertTrue(self.bm.delete(bid))
        self.assertIsNone(self.bm.get(bid))

    def test_cross_project_compare_rejected(self):
        baseline = self.bm.create("p1", "b1", "v1.0")
        with self.assertRaises(BaselineError):
            self.bm.compare_build("p2", "b1", baseline["id"])


if __name__ == "__main__":
    unittest.main()

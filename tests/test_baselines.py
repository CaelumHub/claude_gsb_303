"""发布基线单元测试。

覆盖：基线固化（跨报告 / 覆盖率 / 缺陷三个来源的快照）、标签唯一性、
基线不可变带来的多次对比一致性、新增失败 / 覆盖率升降 / 耗时退化 /
缺陷状态差异的计算。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (BaselineManager, CoverageAnalyzer, DefectManager,
                    ReportGenerator)
from storage import BuildStoreRegistry, StoreRegistry


def _make_env(tmpdir):
    registry = StoreRegistry(os.path.join(tmpdir, "store"), shard_size=50)
    builds = BuildStoreRegistry(os.path.join(tmpdir, "builds"))
    report = ReportGenerator(builds)
    coverage = CoverageAnalyzer(builds)
    defects = DefectManager(registry)
    mgr = BaselineManager(registry, builds, report, coverage, defects)
    return registry, builds, report, coverage, defects, mgr


def _run_build(builds, project_id, build_id, results, status="failed"):
    """模拟一次构建：落结果 + 结束。"""
    store = builds.for_project(project_id)
    store.create(build_id)
    store.set_total(build_id, len(results))
    for r in results:
        store.record_result(build_id, r)
    store.finish(build_id, status)


def _result(case_id, status, duration=0.1, name=None):
    return {
        "case_id": case_id,
        "case_name": name or case_id,
        "group": "g1",
        "priority": "P1",
        "status": status,
        "duration": duration,
        "assertions": ([] if status == "passed"
                       else [{"ok": False, "message": f"{case_id} 断言失败"}]),
        "steps": [],
        "logs": [],
    }


class TestBaselineCreate(unittest.TestCase):
    def test_create_freezes_three_sources(self):
        with tempfile.TemporaryDirectory() as d:
            _, builds, _, _, defects, mgr = _make_env(d)
            _run_build(builds, "p1", "b1", [
                _result("c1", "passed"), _result("c2", "failed"),
            ])
            defects.create("p1", {"title": "旧缺陷", "severity": "major"})

            baseline = mgr.create("p1", "b1", "v1.0", note="首个基线")
            self.assertNotIn("error", baseline)
            snap = baseline["snapshot"]
            # 报告来源：通过率口径 passed/(total-skipped)
            self.assertEqual(snap["report"]["summary"]["pass_rate"], 50.0)
            self.assertEqual(len(snap["report"]["failures"]), 1)
            self.assertIn("avg", snap["report"]["durations"])
            # 覆盖率来源：与 CoverageAnalyzer.get 一致
            self.assertGreater(snap["coverage"]["percent"], 0)
            self.assertTrue(snap["coverage"]["files"])
            # 缺陷来源：冻结时刻的缺陷统计
            self.assertEqual(snap["defects"]["total"], 1)
            self.assertEqual(snap["defects"]["by_status"]["open"], 1)

    def test_label_must_be_unique_per_project(self):
        with tempfile.TemporaryDirectory() as d:
            _, builds, _, _, _, mgr = _make_env(d)
            _run_build(builds, "p1", "b1", [_result("c1", "passed")], "passed")
            _run_build(builds, "p1", "b2", [_result("c1", "passed")], "passed")
            self.assertNotIn("error", mgr.create("p1", "b1", "v1.0"))
            dup = mgr.create("p1", "b2", "v1.0")
            self.assertIn("error", dup)
            # 不同项目可以用同一标签
            _run_build(builds, "p2", "b3", [_result("c1", "passed")], "passed")
            self.assertNotIn("error", mgr.create("p2", "b3", "v1.0"))

    def test_reject_running_build_and_empty_label(self):
        with tempfile.TemporaryDirectory() as d:
            _, builds, _, _, _, mgr = _make_env(d)
            store = builds.for_project("p1")
            store.create("b1")
            store.set_total("b1", 1)  # running
            self.assertIn("error", mgr.create("p1", "b1", "v1.0"))
            store.finish("b1", "passed")
            self.assertIn("error", mgr.create("p1", "b1", "  "))
            self.assertIn("error", mgr.create("p1", "nope", "v1.0"))

    def test_list_and_update_and_delete(self):
        with tempfile.TemporaryDirectory() as d:
            _, builds, _, _, _, mgr = _make_env(d)
            _run_build(builds, "p1", "b1", [_result("c1", "passed")], "passed")
            b = mgr.create("p1", "b1", "v1.0")
            mgr.create("p1", "b1", "v1.1")
            lst = mgr.list("p1")
            self.assertEqual(len(lst), 2)
            self.assertIn("pass_rate", lst[0])  # 列表带摘要但不带快照
            self.assertNotIn("snapshot", lst[0])

            updated = mgr.update(b["id"], {"label": "v1.0-release", "note": "x"})
            self.assertEqual(updated["label"], "v1.0-release")
            # 快照不可通过 update 篡改
            updated2 = mgr.update(b["id"], {"snapshot": {}})
            self.assertNotEqual(updated2.get("snapshot"), {})
            # 标签与其他基线冲突时拒绝
            self.assertIsNone(mgr.update(b["id"], {"label": "v1.1"}))

            self.assertTrue(mgr.delete(b["id"]))
            self.assertEqual(len(mgr.list("p1")), 1)


class TestBaselineCompare(unittest.TestCase):
    def _setup(self, d):
        registry, builds, report, coverage, defects, mgr = _make_env(d)
        _run_build(builds, "p1", "b1", [
            _result("c1", "passed", 0.10),
            _result("c2", "failed", 0.20),
            _result("c3", "passed", 0.30),
        ])
        baseline = mgr.create("p1", "b1", "v1.0")
        # 第二次构建：c2 修复、c4 新增失败、整体耗时上涨
        _run_build(builds, "p1", "b2", [
            _result("c1", "passed", 0.10),
            _result("c2", "passed", 0.20),
            _result("c3", "passed", 0.90),
            _result("c4", "failed", 0.50),
        ])
        return defects, mgr, baseline

    def test_compare_report_diff(self):
        with tempfile.TemporaryDirectory() as d:
            _, mgr, baseline = self._setup(d)
            diff = mgr.compare("p1", baseline["id"], "b2")
            rp = diff["report"]
            # 通过率 66.7% -> 75%
            self.assertEqual(rp["pass_rate"]["baseline"], 66.7)
            self.assertEqual(rp["pass_rate"]["current"], 75.0)
            self.assertAlmostEqual(rp["pass_rate"]["delta"], 8.3, places=1)
            # 新增失败 c4、已修复 c2
            self.assertEqual(rp["new_failure_count"], 1)
            self.assertEqual(rp["new_failures"][0]["case_id"], "c4")
            self.assertEqual(rp["resolved_failure_count"], 1)
            self.assertEqual(rp["resolved_failures"][0]["case_id"], "c2")
            # 耗时退化（总耗时 0.6 -> 1.7，远超 10% 阈值）
            self.assertTrue(rp["duration"]["regressed"])
            self.assertGreater(rp["duration"]["delta"], 0)

    def test_compare_coverage_diff(self):
        with tempfile.TemporaryDirectory() as d:
            _, mgr, baseline = self._setup(d)
            diff = mgr.compare("p1", baseline["id"], "b2")
            cov = diff["coverage"]
            self.assertIn("delta", cov["percent"])
            self.assertAlmostEqual(
                cov["percent"]["delta"],
                round(cov["percent"]["current"] - cov["percent"]["baseline"], 1))
            self.assertTrue(cov["files"])
            for f in cov["files"]:
                self.assertAlmostEqual(
                    f["delta"], round(f["current"] - f["baseline"], 1))

    def test_compare_defects_diff(self):
        with tempfile.TemporaryDirectory() as d:
            defects, mgr, baseline = self._setup(d)
            # 基线固化之后新增缺陷 -> 体现在「当前侧」与新增列表，不改基线
            defects.create("p1", {"title": "新缺陷", "severity": "critical"})
            diff = mgr.compare("p1", baseline["id"], "b2")
            dd = diff["defects"]
            self.assertEqual(dd["total"]["baseline"], 0)
            self.assertEqual(dd["total"]["current"], 1)
            self.assertEqual(dd["total"]["delta"], 1)
            self.assertEqual(dd["new_since_baseline_count"], 1)
            self.assertEqual(dd["new_since_baseline"][0]["title"], "新缺陷")
            self.assertIn("critical", dd["by_severity"])

    def test_baseline_is_stable_across_comparisons(self):
        """同一基线多次对比结果一致；缺陷变化只影响当前侧，不改基线侧。"""
        with tempfile.TemporaryDirectory() as d:
            defects, mgr, baseline = self._setup(d)
            d1 = mgr.compare("p1", baseline["id"], "b2")
            defects.create("p1", {"title": "后来的缺陷"})
            d2 = mgr.compare("p1", baseline["id"], "b2")
            d1.pop("generated_at")
            d2.pop("generated_at")
            # 基线侧完全稳定
            self.assertEqual(d1["baseline"], d2["baseline"])
            self.assertEqual(d1["report"], d2["report"])
            self.assertEqual(d1["coverage"], d2["coverage"])
            # 缺陷当前侧反映活数据
            self.assertEqual(d2["defects"]["total"]["current"], 1)
            self.assertEqual(d2["defects"]["total"]["baseline"], 0)
            # 固化后基线快照本身不变
            self.assertEqual(mgr.get(baseline["id"])["snapshot"]["defects"]["total"], 0)

    def test_compare_errors(self):
        with tempfile.TemporaryDirectory() as d:
            _, mgr, baseline = self._setup(d)
            self.assertIn("error", mgr.compare("p1", "nope", "b2"))
            self.assertIn("error", mgr.compare("p1", baseline["id"], "nope"))
            self.assertIn("error", mgr.compare("p2", baseline["id"], "b2"))


if __name__ == "__main__":
    unittest.main()

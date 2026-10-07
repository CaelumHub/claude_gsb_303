"""发布基线：把一次构建的全套结果固化为可对比的发布基线。

一次「发布质量」结论横跨三个生成时机、统计口径各不相同的数据源，只取
其中一两个会和报告页 / 覆盖率页对不上：

- **通过率 / 失败明细 / 耗时分布** 来自构建结果（``build.json`` 聚合计数
  与结果分片，报告页展示的口径，见 :mod:`engine.report`）；
- **覆盖率** 来自独立的覆盖率快照（``coverage.json``，构建收尾时生成、
  可被显式重算，见 :mod:`engine.coverage`）；
- **缺陷状态** 来自缺陷列表（缺陷会在构建之后继续流转，见
  :mod:`engine.defects`）。

因此基线在创建的一瞬间，从这三个来源**各取一份当时的快照**整体固化下来：

- 之后构建结果、覆盖率快照、缺陷列表再怎么变化（重算 / 缺陷流转），
  基线内容都不再改变 —— 同一基线无论何时、与哪个构建对比，结果一致；
- 对比时同样分别从「当前构建的三来源」现取数据，再与快照逐项相减，
  口径与报告页 / 覆盖率页完全一致，不会出现「页面上是 A、对比是 B」。

基线按项目维护，带可读标签（如版本号 ``v2.3.0``），仅允许基于**已结束**
的构建固化（运行中的构建数据还在变，固化没有意义）。
"""

from __future__ import annotations

import time
from typing import Optional

from .models import new_id
from .report import FAILED_STATUSES

# 失败明细 / 最慢用例在快照里保留的条数上限（与报告页 top-N 口径对齐，
# 同时避免把超大结果集整体塞进基线记录）。
_FAILURE_LIMIT = 200
_SLOWEST_LIMIT = 50


class BaselineError(ValueError):
    """基线操作的预期内错误（标签重复 / 构建未结束 / 跨项目等）。"""


class BaselineManager:
    """发布基线管理：固化快照、列出 / 改名 / 删除、计算多源差异。"""

    def __init__(self, registry, build_registry, report_gen, coverage_analyzer,
                 defect_manager):
        self._store = registry.store("baselines")
        self.builds = build_registry
        self.report_gen = report_gen
        self.coverage = coverage_analyzer
        self.defects = defect_manager

    # ------------------------------------------------------------------ 固化
    def create(self, project_id: str, build_id: str, tag: str,
               description: str = "") -> dict:
        """把一场已结束构建的全套结果固化为一条发布基线。

        三个数据源在同一临界动作里依次取快照并整体落一条基线记录，
        记录里再带上每个来源的采集时间，保证「时间点与来源稳定」。
        """
        tag = (tag or "").strip()
        if not tag:
            raise BaselineError("基线标签不能为空（如版本号 v2.3.0）")
        store = self.builds.for_project(project_id)
        build = store.get(build_id)
        if build is None:
            raise BaselineError("构建不存在")
        if build.get("status") in ("pending", "running"):
            raise BaselineError("构建尚未结束，不能固化为基线")
        if self.get_by_tag(project_id, tag) is not None:
            raise BaselineError(f"标签 {tag!r} 已被本项目的另一条基线占用")

        snapshot = self._snapshot(project_id, build_id, build)
        frozen_at = time.time()
        baseline = {
            "id": new_id("base"),
            "project_id": project_id,
            "tag": tag,
            "description": description or "",
            "build_id": build_id,
            "build_name": build.get("name") or build_id,
            "build_status": build.get("status"),
            "trigger": build.get("trigger"),
            "frozen_at": frozen_at,
            "snapshot": snapshot,
        }
        self._store.insert(baseline)
        return baseline

    def _snapshot(self, project_id: str, build_id: str,
                  build: Optional[dict] = None) -> dict:
        """采集一次构建的三来源快照（报告 + 覆盖率 + 缺陷）。"""
        store = self.builds.for_project(project_id)
        if build is None:
            build = store.get(build_id)

        # 来源 1：构建结果 —— 按当前结果计算一份报告（不走缓存，也不回写
        # report.json，固化动作不改动其它数据源），连同结果级失败 / 耗时
        # 明细一起固化。
        report = self.report_gen.compute_report(project_id, build_id)
        if "error" in report:
            raise BaselineError(report["error"])
        all_results = store.results(build_id)
        failures = [r for r in all_results
                    if r.get("status") in FAILED_STATUSES]
        failures_snapshot = [self._failure_entry(r)
                             for r in failures[:_FAILURE_LIMIT]]
        slowest_snapshot = [
            {"case_id": r.get("case_id"), "case_name": r.get("case_name"),
             "group": r.get("group"), "status": r.get("status"),
             "duration": r.get("duration")}
            for r in sorted(all_results,
                            key=lambda r: r.get("duration") or 0.0,
                            reverse=True)[:_SLOWEST_LIMIT]
        ]

        # 来源 2：覆盖率快照。已生成则原样固化（与覆盖率页看到的一致），
        # 尚未生成则现取（覆盖率页本身也是这个惰性生成口径）。
        cov = self.coverage.get(project_id, build_id)

        # 来源 3：缺陷列表在「固化这一刻」的状态快照。只冻结对比所需字段，
        # 缺陷后续继续流转（fixed/closed/reopened…）不影响基线。
        defect_rows = self.defects.list(project_id)
        defects_snapshot = [{
            "id": d.get("id"),
            "title": d.get("title"),
            "severity": d.get("severity", "major"),
            "status": d.get("status", "open"),
            "source_case_id": d.get("source_case_id"),
            "source_build_id": d.get("source_build_id"),
        } for d in defect_rows]

        return {
            "report": {
                "summary": report.get("summary", {}),
                "durations": report.get("durations", {}),
                "by_group": report.get("by_group", {}),
                "by_priority": report.get("by_priority", {}),
                "build_duration": report.get("duration", 0.0),
                "failures": failures_snapshot,
                "failure_case_ids": sorted(
                    f.get("case_id") for f in failures_snapshot
                    if f.get("case_id")),
                "slowest": slowest_snapshot,
            },
            "coverage": self._coverage_snapshot(cov),
            "defects": {
                "rows": defects_snapshot,
                "stats": self._defect_stats(defects_snapshot),
            },
            "sources": {
                # 三个来源各自的采集时间，固化后不再变化；对比现取数据时
                # 与这里的冻结值对照，可清楚说明「比的是哪个时间点」。
                "report_frozen_at": time.time(),
                "coverage_frozen_at": (cov or {}).get("generated_at"),
                "defects_frozen_at": time.time(),
                "failure_limit": _FAILURE_LIMIT,
            },
        }

    @staticmethod
    def _coverage_snapshot(cov: Optional[dict]) -> dict:
        cov = cov or {}
        return {
            "percent": cov.get("percent", 0.0),
            "total_lines": cov.get("total_lines", 0),
            "covered_lines": cov.get("covered_lines", 0),
            "missed_lines": cov.get("missed_lines", 0),
            "generated_at": cov.get("generated_at"),
            "files": [
                {"file": f.get("file"), "lines": f.get("lines", 0),
                 "covered": f.get("covered", 0), "missed": f.get("missed", 0),
                 "percent": f.get("percent", 0.0)}
                for f in cov.get("files", [])
            ],
        }

    @staticmethod
    def _failure_entry(r: dict) -> dict:
        failing_assert = next((a for a in r.get("assertions", [])
                               if not a.get("ok")), None)
        failing_step = next((s for s in r.get("steps", [])
                             if s.get("status") in ("failed", "error")), None)
        return {
            "case_id": r.get("case_id"),
            "case_name": r.get("case_name"),
            "status": r.get("status"),
            "group": r.get("group"),
            "priority": r.get("priority"),
            "duration": r.get("duration"),
            "reason": ((failing_assert or {}).get("message")
                       or (failing_step or {}).get("message")
                       or r.get("message") or ""),
        }

    @staticmethod
    def _defect_stats(rows: list[dict]) -> dict:
        by_status: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for d in rows:
            st = d.get("status", "open")
            sv = d.get("severity", "major")
            by_status[st] = by_status.get(st, 0) + 1
            by_severity[sv] = by_severity.get(sv, 0) + 1
        return {"total": len(rows), "by_status": by_status,
                "by_severity": by_severity}

    # ------------------------------------------------------------------ 查询
    def list(self, project_id: str) -> list[dict]:
        """列出项目下全部基线（不含可能很大的快照明细，仅摘要）。"""
        rows = self._store.query(where=[("project_id", "eq", project_id)],
                                 order_by="frozen_at", order="desc")
        return [BaselineManager.summary(b) for b in rows]

    def get(self, baseline_id: str) -> Optional[dict]:
        return self._store.get(baseline_id)

    def get_by_tag(self, project_id: str, tag: str) -> Optional[dict]:
        rows = self._store.query(
            where=[("project_id", "eq", project_id), ("tag", "eq", tag)])
        return rows[0] if rows else None

    @staticmethod
    def summary(b: dict) -> dict:
        snap = b.get("snapshot", {})
        return {
            "id": b.get("id"),
            "project_id": b.get("project_id"),
            "tag": b.get("tag"),
            "description": b.get("description", ""),
            "build_id": b.get("build_id"),
            "build_name": b.get("build_name"),
            "build_status": b.get("build_status"),
            "trigger": b.get("trigger"),
            "frozen_at": b.get("frozen_at"),
            "pass_rate": (snap.get("report", {}).get("summary", {})
                          .get("pass_rate", 0.0)),
            "coverage_percent": snap.get("coverage", {}).get("percent", 0.0),
            "defect_total": snap.get("defects", {}).get("stats", {})
                            .get("total", 0),
        }

    def update_meta(self, baseline_id: str, patch: dict) -> dict:
        baseline = self.get(baseline_id)
        if baseline is None:
            raise BaselineError("基线不存在")
        updates = {}
        if "tag" in patch:
            tag = (patch.get("tag") or "").strip()
            if not tag:
                raise BaselineError("基线标签不能为空")
            clash = self.get_by_tag(baseline["project_id"], tag)
            if clash is not None and clash["id"] != baseline_id:
                raise BaselineError(f"标签 {tag!r} 已被另一条基线占用")
            updates["tag"] = tag
        if "description" in patch:
            updates["description"] = patch.get("description") or ""
        if not updates:
            return baseline
        updated = self._store.update(baseline_id, updates)
        return updated

    def delete(self, baseline_id: str) -> bool:
        return self._store.delete(baseline_id)

    # ------------------------------------------------------------------ 对比
    def compare_build(self, project_id: str, build_id: str,
                      baseline_id: str) -> dict:
        """当前构建（三来源现取）相对历史基线快照的多源差异。

        基线侧全部读固化快照；当前侧重新采集一次（与报告页 / 覆盖率页 /
        缺陷页此刻展示的数据同口径）。基线不变、当前构建也已结束时，
        多次调用结果一致。
        """
        baseline = self.get(baseline_id)
        if baseline is None:
            raise BaselineError("基线不存在")
        if baseline.get("project_id") != project_id:
            raise BaselineError("基线不属于该项目")
        build = self.builds.for_project(project_id).get(build_id)
        if build is None:
            raise BaselineError("构建不存在")

        current = self._snapshot(project_id, build_id, build)
        base_snap = baseline["snapshot"]
        return {
            "project_id": project_id,
            "build_id": build_id,
            "build_name": build.get("name") or build_id,
            "build_status": build.get("status"),
            "baseline": BaselineManager.summary(baseline),
            # 基线固化时间与各来源快照时间（稳定不变）；当前侧时间点由构建
            # finished_at 与覆盖率快照 generated_at 等「数据自带时间」表达，
            # 不写对比生成时刻，保证多次对比结果逐字段一致。
            "frozen_at": baseline.get("frozen_at"),
            "base_sources": base_snap.get("sources", {}),
            "summary": self._diff_summary(base_snap, current),
            "coverage": self._diff_coverage(base_snap, current),
            "durations": self._diff_durations(base_snap, current),
            "failures": self._diff_failures(base_snap, current),
            "defects": self._diff_defects(base_snap, current),
            "by_group": self._diff_groups(base_snap, current),
        }

    def compare_coverage(self, project_id: str, build_id: str,
                         baseline_id: str) -> dict:
        """覆盖率页专用：只取覆盖率维度的差异（总体 + 每文件）。"""
        full = self.compare_build(project_id, build_id, baseline_id)
        return {
            "project_id": project_id,
            "build_id": build_id,
            "build_name": full["build_name"],
            "baseline": full["baseline"],
            "frozen_at": full["frozen_at"],
            "coverage": full["coverage"],
        }

    # -- 各维度差异 --------------------------------------------------------
    def _diff_summary(self, base: dict, cur: dict) -> dict:
        b = base.get("report", {}).get("summary", {})
        c = cur.get("report", {}).get("summary", {})

        def num(d, k):
            try:
                return float(d.get(k) or 0)
            except (TypeError, ValueError):
                return 0.0

        keys = ("total", "passed", "failed", "error", "skipped",
                "timeout", "pass_rate")
        out = {}
        for k in keys:
            bv, cv = num(b, k), num(c, k)
            out[k] = {"baseline": bv, "current": cv, "delta": round(cv - bv, 1)}
        # 「失败总数」沿用报告页口径：failed + error + timeout
        bf = num(b, "failed") + num(b, "error") + num(b, "timeout")
        cf = num(c, "failed") + num(c, "error") + num(c, "timeout")
        out["fail_count"] = {"baseline": bf, "current": cf,
                             "delta": int(cf - bf)}
        # 构建总耗时
        bd = base.get("report", {}).get("build_duration", 0.0)
        cd = cur.get("report", {}).get("build_duration", 0.0)
        out["build_duration"] = {"baseline": bd, "current": cd,
                                 "delta": round(cd - bd, 3)}
        return out

    def _diff_coverage(self, base: dict, cur: dict) -> dict:
        b = base.get("coverage", {})
        c = cur.get("coverage", {})
        files_b = {f.get("file"): f for f in b.get("files", [])}
        files_c = {f.get("file"): f for f in c.get("files", [])}
        files = []
        for name in sorted(files_b.keys() | files_c.keys()):
            fb, fc = files_b.get(name, {}), files_c.get(name, {})
            bp = float(fb.get("percent", 0.0) or 0.0)
            cp = float(fc.get("percent", 0.0) or 0.0)
            files.append({
                "file": name,
                "baseline_percent": bp if fb else None,
                "current_percent": cp if fc else None,
                "delta": round(cp - bp, 1) if (fb and fc) else None,
            })
        bp = float(b.get("percent", 0.0) or 0.0)
        cp = float(c.get("percent", 0.0) or 0.0)
        return {
            "percent": {"baseline": bp, "current": cp,
                        "delta": round(cp - bp, 1)},
            "covered_lines": {
                "baseline": b.get("covered_lines", 0),
                "current": c.get("covered_lines", 0),
                "delta": c.get("covered_lines", 0) - b.get("covered_lines", 0)},
            "files": files,
        }

    def _diff_durations(self, base: dict, cur: dict) -> dict:
        b = base.get("report", {}).get("durations", {})
        c = cur.get("report", {}).get("durations", {})
        keys = ("avg", "median", "p95", "p99", "max", "min")
        out = {}
        for k in keys:
            bv = float(b.get(k) or 0.0)
            cv = float(c.get(k) or 0.0)
            out[k] = {"baseline": bv, "current": cv,
                      "delta": round(cv - bv, 3)}
        return out

    def _diff_failures(self, base: dict, cur: dict) -> dict:
        b_rep = base.get("report", {})
        c_rep = cur.get("report", {})
        b_ids = set(b_rep.get("failure_case_ids", []))
        c_rows = c_rep.get("failures", [])
        b_rows = b_rep.get("failures", [])
        c_by_id = {f.get("case_id"): f for f in c_rows if f.get("case_id")}
        b_by_id = {f.get("case_id"): f for f in b_rows if f.get("case_id")}
        c_ids = set(c_by_id)

        def detail(fid, source):
            return source.get(fid, {"case_id": fid, "case_name": fid})

        return {
            "new": [detail(fid, c_by_id) for fid in sorted(c_ids - b_ids)],
            "fixed": [detail(fid, b_by_id) for fid in sorted(b_ids - c_ids)],
            "persistent": [detail(fid, c_by_id) for fid in sorted(b_ids & c_ids)],
            "new_count": len(c_ids - b_ids),
            "fixed_count": len(b_ids - c_ids),
            "truncated": (len(b_ids) >= _FAILURE_LIMIT
                          or len(c_rows) >= _FAILURE_LIMIT),
        }

    def _diff_defects(self, base: dict, cur: dict) -> dict:
        b_rows = {d.get("id"): d for d in base.get("defects", {}).get("rows", [])}
        c_rows = {d.get("id"): d
                  for d in self._snapshot_defect_rows(cur)}
        opened, closed, changed = [], [], []
        # 当前相对基线新增的缺陷
        for did in sorted(c_rows.keys() - b_rows.keys()):
            opened.append(c_rows[did])
        # 基线里有、当前列表里没有的缺陷（被删除；状态流本身不会让缺陷消失）
        for did in sorted(b_rows.keys() - c_rows.keys()):
            closed.append({"id": did, "title": b_rows[did].get("title"),
                           "baseline_status": b_rows[did].get("status"),
                           "current_status": None})
        # 状态发生流转的缺陷
        for did in sorted(b_rows.keys() & c_rows.keys()):
            bs, cs = b_rows[did].get("status"), c_rows[did].get("status")
            if bs != cs:
                row = dict(c_rows[did])
                row["baseline_status"] = bs
                row["current_status"] = cs
                changed.append(row)
        b_stats = base.get("defects", {}).get("stats", {})
        c_stats = self._defect_stats(list(c_rows.values()))
        return {
            "opened": opened,
            "removed": closed,
            "status_changed": changed,
            "stats": {
                "baseline": b_stats,
                "current": c_stats,
                "total_delta": c_stats.get("total", 0)
                               - b_stats.get("total", 0),
            },
        }

    @staticmethod
    def _snapshot_defect_rows(cur_snapshot: dict) -> list[dict]:
        return cur_snapshot.get("defects", {}).get("rows", [])

    def _diff_groups(self, base: dict, cur: dict) -> dict:
        b = base.get("report", {}).get("by_group", {})
        c = cur.get("report", {}).get("by_group", {})
        groups = []
        for name in sorted(set(b) | set(c)):
            bg, cg = b.get(name, {}), c.get(name, {})
            bf = int(bg.get("failed", 0) or 0) + int(bg.get("error", 0) or 0) \
                + int(bg.get("timeout", 0) or 0)
            cf = int(cg.get("failed", 0) or 0) + int(cg.get("error", 0) or 0) \
                + int(cg.get("timeout", 0) or 0)
            groups.append({
                "group": name,
                "total": {"baseline": bg.get("total", 0),
                          "current": cg.get("total", 0),
                          "delta": int(cg.get("total", 0) - bg.get("total", 0))},
                "failed": {"baseline": bf, "current": cf,
                           "delta": cf - bf},
            })
        return {"groups": groups}

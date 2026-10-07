"""发布基线：把一次构建的全套结果固化为可对比的基线。

为什么需要基线
--------------
「这次发布到底比上次差在哪」靠眼睛在两份报告之间来回扫是说不清的。
基线把一次构建在某一时刻的全套结果**冻结**下来，后续构建任选一条历史
基线对比，直接看出：新增了多少失败、覆盖率升降了多少、耗时是否退化、
缺陷积压变化。

跨来源的一致性（本模块的核心约束）
--------------------------------
一次对比横跨三个生成时机与统计口径都不同的数据源：

- 通过率 / 耗时分布 / 失败明细 → :class:`~engine.report.ReportGenerator`
  （口径：``pass_rate = passed / (total - skipped)``，与报告页完全一致）；
- 覆盖率 → :class:`~engine.coverage.CoverageAnalyzer` 的独立覆盖率快照
  （按构建确定性生成，与覆盖率页完全一致）；
- 缺陷状态 → :class:`~engine.defects.DefectManager` 的缺陷列表
  （缺陷是活数据，状态会随处理流转）。

只取其中一两个来源、或各自用不同口径现算，就会得出和报告页对不上的
结论。因此本模块保证：

1. **固化时同源**：基线快照里的报告、覆盖率、缺陷统计分别来自上述三个
   管理器在冻结时刻的输出，原样落盘，不再重算；
2. **基线不可变**：基线创建后快照不再变化（仅标签 / 备注可改），
   同一基线固化的时间点与数据来源稳定，多次对比结果一致；
3. **对比时同口径**：「当前侧」也从同样三个管理器现取，与基线侧逐项
   对齐比较，差异结论与报告页 / 覆盖率页看到的数据同源。

存储
----
基线是项目级实体，存于分片存储的 ``baselines`` 集合，一个项目可维护
多条（按标签区分，如版本号 ``v2.31.0``）；同一项目内标签唯一。
"""

from __future__ import annotations

import time
from typing import Optional

from .models import new_id

# 耗时退化判定阈值：总耗时或 P95 相对基线上涨超过该比例即视为退化
DURATION_REGRESSION_PCT = 10.0

# 对比中缺陷明细 / 新增失败明细的最大返回条数
_MAX_DETAIL = 50


class BaselineManager:
    """发布基线管理：固化、查询与跨来源对比。"""

    def __init__(self, registry, build_registry, report_gen,
                 coverage_analyzer, defect_manager):
        self._store = registry.store("baselines")
        self.builds = build_registry
        self.report_gen = report_gen
        self.coverage = coverage_analyzer
        self.defects = defect_manager

    # ------------------------------------------------------------------ 固化
    def create(self, project_id: str, build_id: str, label: str,
               note: str = "") -> dict:
        """把指定构建当前的全套结果固化为基线。

        快照内容（三个来源各取一次，原样冻结）：

        - ``report``   ：报告生成器输出（通过率 / 计数 / 耗时分布 / 失败明细）；
        - ``coverage`` ：覆盖率快照（总覆盖率 + 每文件覆盖率）；
        - ``defects``  ：缺陷状态统计（按状态 / 按严重级别计数）。
        """
        label = (label or "").strip()
        if not label:
            return {"error": "基线标签不能为空"}
        build = self.builds.for_project(project_id).get(build_id)
        if build is None:
            return {"error": "构建不存在"}
        if build.get("status") in ("pending", "running"):
            return {"error": "构建尚未结束，不能固化为基线"}
        for b in self.list(project_id):
            if b.get("label") == label:
                return {"error": f"标签 {label} 已存在，同一项目内标签需唯一"}

        report = self.report_gen.build_report(project_id, build_id)
        if "error" in report:
            return {"error": report["error"]}
        coverage = self.coverage.get(project_id, build_id)
        defect_stats = self.defects.stats(project_id)

        baseline = {
            "id": new_id("base"),
            "project_id": project_id,
            "build_id": build_id,
            "build_name": build.get("name") or build_id,
            "label": label,
            "note": note or "",
            "created_at": time.time(),
            "snapshot": {
                "report": {
                    "summary": report.get("summary", {}),
                    "durations": report.get("durations", {}),
                    "duration": report.get("duration", 0.0),
                    "status": report.get("status"),
                    "failures": report.get("failures", []),
                    "by_group": report.get("by_group", {}),
                    "finished_at": report.get("finished_at"),
                },
                "coverage": {
                    "percent": coverage.get("percent", 0.0),
                    "total_lines": coverage.get("total_lines", 0),
                    "covered_lines": coverage.get("covered_lines", 0),
                    "files": [{"file": f.get("file"), "percent": f.get("percent"),
                               "covered": f.get("covered"), "lines": f.get("lines")}
                              for f in coverage.get("files", [])],
                },
                "defects": defect_stats,
            },
        }
        self._store.insert(baseline)
        return baseline

    # ------------------------------------------------------------------ 查询
    def list(self, project_id: str) -> list[dict]:
        """列出项目的全部基线（不含快照明细，按创建时间倒序）。"""
        baselines = self._store.query(
            where=[("project_id", "eq", project_id)],
            order_by="created_at", order="desc")
        out = []
        for b in baselines:
            snap = b.get("snapshot", {})
            out.append({
                "id": b["id"],
                "project_id": b["project_id"],
                "build_id": b.get("build_id"),
                "build_name": b.get("build_name"),
                "label": b.get("label"),
                "note": b.get("note", ""),
                "created_at": b.get("created_at"),
                "pass_rate": snap.get("report", {}).get("summary", {}).get("pass_rate"),
                "coverage_percent": snap.get("coverage", {}).get("percent"),
                "defect_total": snap.get("defects", {}).get("total"),
            })
        return out

    def get(self, baseline_id: str) -> Optional[dict]:
        return self._store.get(baseline_id)

    def update(self, baseline_id: str, patch: dict) -> Optional[dict]:
        """仅允许改标签 / 备注；快照一旦固化不可变。"""
        allowed = {k: patch[k] for k in ("label", "note") if k in patch}
        if "label" in allowed:
            allowed["label"] = (allowed["label"] or "").strip()
            if not allowed["label"]:
                return None
            baseline = self._store.get(baseline_id)
            if baseline:
                for b in self.list(baseline["project_id"]):
                    if b.get("label") == allowed["label"] and b["id"] != baseline_id:
                        return None
        return self._store.update(baseline_id, allowed)

    def delete(self, baseline_id: str) -> bool:
        return self._store.delete(baseline_id)

    # ------------------------------------------------------------------ 对比
    def compare(self, project_id: str, baseline_id: str, build_id: str) -> dict:
        """把一次构建与指定基线对比，返回跨来源的差异报告。"""
        baseline = self._store.get(baseline_id)
        if baseline is None or baseline.get("project_id") != project_id:
            return {"error": "基线不存在"}
        build = self.builds.for_project(project_id).get(build_id)
        if build is None:
            return {"error": "构建不存在"}

        snap = baseline.get("snapshot", {})
        base_report = snap.get("report", {})
        base_cov = snap.get("coverage", {})
        base_defects = snap.get("defects", {})

        # 当前侧：与基线侧完全同源同口径现取
        cur_report = self.report_gen.build_report(project_id, build_id)
        if "error" in cur_report:
            return {"error": cur_report["error"]}
        cur_cov = self.coverage.get(project_id, build_id)
        cur_defects = self.defects.stats(project_id)

        return {
            "baseline": {k: baseline.get(k) for k in
                         ("id", "label", "note", "build_id", "build_name", "created_at")},
            "build": {
                "id": build_id,
                "name": build.get("name") or build_id,
                "status": build.get("status"),
                "finished_at": build.get("finished_at"),
            },
            "report": self._diff_report(base_report, cur_report),
            "coverage": self._diff_coverage(base_cov, cur_cov),
            "defects": self._diff_defects(base_defects, cur_defects,
                                          project_id, baseline.get("created_at", 0)),
            "generated_at": time.time(),
        }

    # -- 报告侧差异（通过率 / 失败 / 耗时） ---------------------------------
    def _diff_report(self, base: dict, cur: dict) -> dict:
        bs = base.get("summary", {})
        cs = cur.get("summary", {})
        bd = base.get("durations", {})
        cd = cur.get("durations", {})

        def metric(base_v, cur_v, digits=1):
            base_v = base_v or 0
            cur_v = cur_v or 0
            return {"baseline": base_v, "current": cur_v,
                    "delta": round(cur_v - base_v, digits)}

        # 失败明细按 case_id 对齐：本次新增失败 / 相对基线已修复
        base_fail = {f.get("case_id"): f for f in base.get("failures", [])}
        cur_fail = {f.get("case_id"): f for f in cur.get("failures", [])}
        new_failures = [f for cid, f in cur_fail.items() if cid not in base_fail]
        resolved = [f for cid, f in base_fail.items() if cid not in cur_fail]

        base_dur = base.get("duration", 0.0) or 0.0
        cur_dur = cur.get("duration", 0.0) or 0.0
        base_p95 = bd.get("p95", 0.0) or 0.0
        cur_p95 = cd.get("p95", 0.0) or 0.0

        def regressed(base_v, cur_v):
            return base_v > 0 and (cur_v - base_v) / base_v * 100 > DURATION_REGRESSION_PCT

        return {
            "pass_rate": metric(bs.get("pass_rate"), cs.get("pass_rate")),
            "total": metric(bs.get("total"), cs.get("total"), 0),
            "passed": metric(bs.get("passed"), cs.get("passed"), 0),
            "failed": metric(
                (bs.get("failed", 0) or 0) + (bs.get("error", 0) or 0) + (bs.get("timeout", 0) or 0),
                (cs.get("failed", 0) or 0) + (cs.get("error", 0) or 0) + (cs.get("timeout", 0) or 0), 0),
            "duration": {**metric(base_dur, cur_dur, 3),
                         "regressed": regressed(base_dur, cur_dur)},
            "durations": {
                "avg": metric(bd.get("avg"), cd.get("avg"), 3),
                "median": metric(bd.get("median"), cd.get("median"), 3),
                "p95": {**metric(base_p95, cur_p95, 3),
                        "regressed": regressed(base_p95, cur_p95)},
                "max": metric(bd.get("max"), cd.get("max"), 3),
            },
            "regression_threshold_pct": DURATION_REGRESSION_PCT,
            "new_failure_count": len(new_failures),
            "resolved_failure_count": len(resolved),
            "new_failures": new_failures[:_MAX_DETAIL],
            "resolved_failures": resolved[:_MAX_DETAIL],
        }

    # -- 覆盖率侧差异 -------------------------------------------------------
    def _diff_coverage(self, base: dict, cur: dict) -> dict:
        base_pct = base.get("percent", 0.0) or 0.0
        cur_pct = cur.get("percent", 0.0) or 0.0
        base_files = {f.get("file"): f for f in base.get("files", [])}
        cur_files = {f.get("file"): f for f in cur.get("files", [])}
        files = []
        for path in sorted(set(base_files) | set(cur_files)):
            b = base_files.get(path, {})
            c = cur_files.get(path, {})
            b_pct = b.get("percent", 0.0) or 0.0
            c_pct = c.get("percent", 0.0) or 0.0
            files.append({"file": path, "baseline": b_pct, "current": c_pct,
                          "delta": round(c_pct - b_pct, 1)})
        # 变化最大的文件排前面，便于一眼看到覆盖率升降来源
        files.sort(key=lambda f: abs(f["delta"]), reverse=True)
        return {
            "percent": {"baseline": base_pct, "current": cur_pct,
                        "delta": round(cur_pct - base_pct, 1)},
            "covered_lines": {
                "baseline": base.get("covered_lines", 0),
                "current": cur.get("covered_lines", 0),
                "delta": (cur.get("covered_lines", 0) or 0) - (base.get("covered_lines", 0) or 0),
            },
            "files": files,
        }

    # -- 缺陷侧差异 ---------------------------------------------------------
    def _diff_defects(self, base: dict, cur: dict,
                      project_id: str, baseline_created_at: float) -> dict:
        def count_diff(base_map, cur_map):
            keys = sorted(set(base_map) | set(cur_map))
            return {k: {"baseline": base_map.get(k, 0),
                        "current": cur_map.get(k, 0),
                        "delta": cur_map.get(k, 0) - base_map.get(k, 0)}
                    for k in keys}

        # 基线固化之后新产生的缺陷（缺陷是活数据，这部分随时间演进）
        new_since = [d for d in self.defects.list(project_id)
                     if d.get("created_at", 0) > baseline_created_at]
        new_since = [{"id": d.get("id"), "title": d.get("title"),
                      "severity": d.get("severity"), "status": d.get("status"),
                      "created_at": d.get("created_at")}
                     for d in new_since[:_MAX_DETAIL]]

        return {
            "total": {"baseline": base.get("total", 0), "current": cur.get("total", 0),
                      "delta": cur.get("total", 0) - base.get("total", 0)},
            "by_status": count_diff(base.get("by_status", {}), cur.get("by_status", {})),
            "by_severity": count_diff(base.get("by_severity", {}), cur.get("by_severity", {})),
            "new_since_baseline": new_since,
            "new_since_baseline_count": len(new_since),
        }

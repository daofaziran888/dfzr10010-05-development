"""表优化执行计划：把优化任务和容量统计编排成不超预算的执行批次。

能力边界：
- 读 iceberg 的优化任务与 stats 的最新容量统计，按表计算优化前后的文件数与容量；
- 默认按收益比例排序，在资源配置总预算内贪心选表；预算不够就跳过当前候选、继续比对剩余候选；
- 同一张表只保留一个活动优化任务，避免容量统计把同一份收益重复计算；
- 计划带版本号：版本冲突处理、重新编排都会让版本 +1，表统计信息与预算核对清单跟着当前版本走；
- 执行完成后只把被选任务的实际效果写回统计信息，未入选任务不产生收益记录。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.services.capacity import ceil_div, format_percent, format_size, parse_size
from app.store import store

TASK_MODULE = "iceberg"
STATS_MODULE = "stats"
PLAN_MODULE = "_iceberg_plans"
CONFIG_MODULE = "_iceberg_plan_config"
CONFLICT_MODULE = "_iceberg_conflicts"
VERSION_MODULE = "_iceberg_plan_versions"
RECONCILE_MODULE = "_iceberg_reconcile"

ACTIVE_TASK_STATUSES = {"待执行", "执行中"}
ACTIVE_PLAN_STATUSES = {"已保存", "版本冲突", "执行中"}
PLAN_STATUSES = ["已保存", "版本冲突", "执行中", "已完成", "已恢复", "已归档"]
SORT_OPTIONS = ["收益比例", "预计收益", "预算占用", "优化前容量", "任务编号", "表名称"]
DEFAULT_SORT = "收益比例"
COMPRESSION_SAVING = 0.15
# 每合并掉一个小文件，回收它占用的行组碎片、索引与元数据开销。
FILE_OVERHEAD = 8 * 1024 ** 2
MAX_SAVING_RATIO = 0.35
DEVIATION_LIMIT = 0.10

_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        text = str(value).replace(",", "").strip()
        return int(float(text)) if text else default
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((_as_int(row.get("id")) for row in rows), default=0) + 1


def _digits(value: Any) -> int:
    """从「PLAN-0007」「ICEB-0004」这类编号里取出尾部序号。"""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return _as_int(digits, 0)


def _number(rows: list[dict[str, Any]], field: str, prefix: str) -> str:
    number = max((_digits(row.get(field)) for row in rows), default=0)
    return f"{prefix}-{number + 1:04d}"


def _sort_key(order: str):
    """排序时给不具备编排条件的候选补零值，避免缺字段的候选把排序打乱。"""

    def normalized(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "不可编排": 0 if item.get("可编排") else 1,
            "预计收益字节": _as_int(item.get("预计收益字节")),
            "预算占用字节": _as_int(item.get("预算占用字节")),
            "优化前容量字节": _as_int(item.get("优化前容量字节")),
            "收益比例值": _as_float(item.get("收益比例值")),
            "表名称": str(item.get("表名称") or ""),
            "任务编号": str(item.get("任务编号") or ""),
            **item,
        }

    if order == "预计收益":
        return lambda item: (normalized(item)["不可编排"], -normalized(item)["预计收益字节"], normalized(item)["表名称"])
    if order == "预算占用":
        return lambda item: (normalized(item)["不可编排"], normalized(item)["预算占用字节"], -normalized(item)["预计收益字节"])
    if order == "优化前容量":
        return lambda item: (normalized(item)["不可编排"], -normalized(item)["优化前容量字节"], normalized(item)["表名称"])
    if order == "任务编号":
        return lambda item: (normalized(item)["不可编排"], normalized(item)["任务编号"], normalized(item)["表名称"])
    if order == "表名称":
        return lambda item: (normalized(item)["不可编排"], normalized(item)["表名称"], normalized(item)["任务编号"])
    return lambda item: (normalized(item)["不可编排"], -normalized(item)["收益比例值"], normalized(item)["表名称"])


class IcebergPlanService:
    """执行计划的编排、版本、执行与核对。"""

    # ------------------------------------------------------------------
    # 预算配置
    # ------------------------------------------------------------------
    def get_config(self) -> dict[str, Any]:
        rows = store.rows(CONFIG_MODULE)
        if not rows:
            rows.append({
                "id": 1,
                "资源总预算": "800 GB",
                "行组大小上限": "512 MB",
                "默认排序": DEFAULT_SORT,
                "配置版本": 1,
                "更新人": "值班管理员",
                "更新时间": _now(),
            })
        config = rows[0]
        budget = parse_size(config.get("资源总预算"), numeric_unit=1024 ** 3, default=0) or 0
        group = parse_size(config.get("行组大小上限"), numeric_unit=1024 ** 2, default=0) or 0
        return {
            "资源总预算": config.get("资源总预算"),
            "资源总预算字节": budget,
            "行组大小上限": config.get("行组大小上限"),
            "行组大小上限字节": group,
            "默认排序": config.get("默认排序") if config.get("默认排序") in SORT_OPTIONS else DEFAULT_SORT,
            "配置版本": _as_int(config.get("配置版本"), 1),
            "更新人": config.get("更新人"),
            "更新时间": config.get("更新时间"),
        }

    def update_config(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with _LOCK:
            config = self.get_config()
            budget_text = values.get("资源总预算")
            group_text = values.get("行组大小上限")
            # 只改其中一项时，另一项沿用当前配置，避免把已生效的预算误清空。
            budget = (parse_size(budget_text, numeric_unit=1024 ** 3, default=None)
                      if budget_text not in (None, "") else config["资源总预算字节"])
            group = (parse_size(group_text, numeric_unit=1024 ** 2, default=None)
                     if group_text not in (None, "") else config["行组大小上限字节"])
            if budget is None or budget <= 0:
                return None, "资源总预算需要填写正数，例如 800 GB"
            if group is None or group <= 0:
                return None, "行组大小上限需要填写正数，例如 512 MB"
            order = str(values.get("默认排序") or config["默认排序"])
            if order not in SORT_OPTIONS:
                return None, f"排序方式「{order}」不在可选范围：{'、'.join(SORT_OPTIONS)}"
            row = store.rows(CONFIG_MODULE)[0]
            row["资源总预算"] = format_size(budget)
            row["行组大小上限"] = format_size(group)
            row["默认排序"] = order
            row["配置版本"] = _as_int(row.get("配置版本"), 1) + 1
            row["更新人"] = str(values.get("更新人") or "值班管理员")
            row["更新时间"] = _now()
            return self.get_config(), "执行计划预算配置已更新"

    # ------------------------------------------------------------------
    # 上游：优化任务 + 容量统计
    # ------------------------------------------------------------------
    def _latest_stats(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in store.rows(STATS_MODULE):
            if str(row.get("status")) == "已清理":
                continue
            table = str(row.get("表名称") or "").strip()
            if not table:
                continue
            candidate = {
                "报告编号": row.get("报告编号"),
                "表名称": table,
                "行总数": _as_int(row.get("行总数")),
                "文件总数": _as_int(row.get("文件总数")),
                "总容量": row.get("总容量"),
                "总容量字节": parse_size(row.get("总容量"), numeric_unit=1024 ** 3, default=0) or 0,
                "统计时间": row.get("统计时间") or "",
                "报告状态": row.get("status"),
            }
            current = latest.get(table)
            if current is None or str(candidate["统计时间"]) >= str(current["统计时间"]):
                latest[table] = candidate
        return latest

    def capacity_stats(self) -> list[dict[str, Any]]:
        """容量统计视图：每张表一行，给出容量、文件数与平均文件大小。"""
        config = self.get_config()
        group = config["行组大小上限字节"]
        items: list[dict[str, Any]] = []
        for table, stats in sorted(self._latest_stats().items()):
            files = stats["文件总数"]
            size = stats["总容量字节"]
            avg = size // files if files else 0
            target = ceil_div(size, group) if group else files
            items.append({
                **stats,
                "平均文件大小": format_size(avg),
                "平均文件大小字节": avg,
                "行组大小上限": config["行组大小上限"],
                "建议文件数": target,
                "小文件数": max(files - target, 0),
                "小文件比例": format_percent(max(files - target, 0) / files) if files else "—",
            })
        return items

    def _active_task_by_table(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """每张表只挑一个活动任务；同表多条时保留最早登记的一条。"""
        active: dict[str, dict[str, Any]] = {}
        duplicates: list[dict[str, Any]] = []
        for row in sorted(store.rows(TASK_MODULE), key=lambda item: _as_int(item.get("id"))):
            if str(row.get("status")) not in ACTIVE_TASK_STATUSES:
                continue
            table = str(row.get("表名称") or "").strip()
            if not table:
                continue
            kept = active.get(table)
            if kept is None:
                active[table] = row
            else:
                duplicates.append({
                    "表名称": table,
                    "保留任务": kept.get("任务编号"),
                    "跳过任务": row.get("任务编号"),
                    "原因": "同表只允许一个活动优化任务，避免容量收益重复计算",
                })
        return active, duplicates

    def _estimate(self, task: dict[str, Any], stats: dict[str, Any], group: int) -> dict[str, Any]:
        """按容量统计估算单表的优化前后文件与容量、预算占用与收益。"""
        if not group:
            return {"可编排": False, "跳过原因": "行组大小上限未配置，无法估算目标文件数"}
        size = stats["总容量字节"]
        files = stats["文件总数"]
        if size <= 0 or files <= 0:
            return {"可编排": False, "跳过原因": "容量统计缺少文件数或总容量，无法估算预算与收益"}
        avg = size // files
        kind = str(task.get("优化类型") or "")
        target_files = max(ceil_div(size, group), 1)
        if kind == "数据压缩":
            # 压缩收益按容量比例估算，文件数不变。
            saving = min(int(size * COMPRESSION_SAVING), int(size * MAX_SAVING_RATIO))
            after_files = files
            cost = size
        elif kind in {"小文件合并", "排序重写"}:
            if files <= target_files:
                return {"可编排": False, "跳过原因": "文件数已达到行组上限对应的目标数，暂无需合并"}
            # 合并只重写小文件，大文件不动；收益按回收的文件碎片开销估算。
            saving = min((files - target_files) * FILE_OVERHEAD, int(size * MAX_SAVING_RATIO))
            after_files = target_files
            cost = min((files - target_files) * avg, size)
        else:
            return {"可编排": False, "跳过原因": f"优化类型「{kind or '未填写'}」不在编排范围，仅支持小文件合并、排序重写与数据压缩"}
        after_size = max(size - saving, 0)
        ratio = saving / cost if cost else 0.0
        return {
            "可编排": True,
            "跳过原因": "",
            "表名称": stats["表名称"],
            "任务编号": task.get("任务编号"),
            "优化类型": kind,
            "任务状态": task.get("status"),
            "行组大小上限": format_size(group),
            "优化前容量": format_size(size),
            "优化前容量字节": size,
            "优化前文件数": files,
            "平均文件大小": format_size(avg),
            "优化后容量": format_size(after_size),
            "优化后容量字节": after_size,
            "优化后文件数": after_files,
            "减少文件数": max(files - after_files, 0),
            "预算占用": format_size(cost),
            "预算占用字节": cost,
            "预计收益": format_size(saving),
            "预计收益字节": saving,
            "收益比例": format_percent(ratio),
            "收益比例值": round(ratio, 4),
        }

    def _reserved_budget(self, *, exclude_plan_id: int | None = None) -> dict[str, int]:
        """已保存/执行中的计划预留的预算，按表汇总，供新计划扣减。"""
        reserved: dict[str, int] = {}
        for plan in store.rows(PLAN_MODULE):
            # 计划行的状态字段是「状态」，不是列表里统一用的 status。
            if str(plan.get("状态")) not in ACTIVE_PLAN_STATUSES:
                continue
            if exclude_plan_id is not None and _as_int(plan.get("id")) == exclude_plan_id:
                continue
            for entry in plan.get("entries", []):
                table = str(entry.get("表名称") or "")
                if table:
                    reserved[table] = _as_int(entry.get("预算占用字节"))
        return reserved

    def candidates(self, *, sort_by: str | None = None, status: str | None = None,
                   exclude_plan_id: int | None = None) -> dict[str, Any]:
        """执行计划候选视图：任务、容量、预算与预计收益放在同一行。"""
        config = self.get_config()
        group = config["行组大小上限字节"]
        order = sort_by if sort_by in SORT_OPTIONS else config["默认排序"]
        stats_map = self._latest_stats()
        active, duplicates = self._active_task_by_table()
        reserved = self._reserved_budget(exclude_plan_id=exclude_plan_id)

        items: list[dict[str, Any]] = []
        for table, task in active.items():
            if status and str(task.get("status")) != status:
                continue
            stats = stats_map.get(table)
            if stats is None:
                items.append({
                    "表名称": table,
                    "任务编号": task.get("任务编号"),
                    "优化类型": task.get("优化类型"),
                    "任务状态": task.get("status"),
                    "可编排": False,
                    "跳过原因": "缺少容量统计，无法估算预算与收益",
                })
                continue
            estimate = self._estimate(task, stats, group)
            estimate["已预留"] = table in reserved
            estimate["已预留字节"] = reserved.get(table, 0)
            items.append(estimate)

        items.sort(key=_sort_key(order))
        reserved_total = sum(reserved.values())
        return {
            "items": items,
            "total": len(items),
            "排序": order,
            "可选排序": SORT_OPTIONS,
            "预算": {
                "资源总预算": config["资源总预算"],
                "资源总预算字节": config["资源总预算字节"],
                "已预留": format_size(reserved_total),
                "已预留字节": reserved_total,
                "可编排预算": format_size(max(config["资源总预算字节"] - reserved_total, 0)),
                "可编排预算字节": max(config["资源总预算字节"] - reserved_total, 0),
            },
            "同表重复任务": duplicates,
        }

    # ------------------------------------------------------------------
    # 编排
    # ------------------------------------------------------------------
    def plan_preview(self, values: dict[str, Any]) -> dict[str, Any]:
        config = self.get_config()
        order = str(values.get("排序") or config["默认排序"])
        if order not in SORT_OPTIONS:
            order = DEFAULT_SORT
        candidates = self.candidates(sort_by=order)
        entries, skipped, spent = self._select(candidates["items"], config["资源总预算字节"],
                                               candidates["预算"]["已预留字节"])
        return {
            "计划编号": "（预览）",
            "版本": 0,
            "状态": "预览",
            "排序": order,
            "行组大小上限": config["行组大小上限"],
            "资源总预算": config["资源总预算"],
            "资源总预算字节": config["资源总预算字节"],
            "已用预算": format_size(spent),
            "已用预算字节": spent,
            "剩余预算": format_size(config["资源总预算字节"] - spent),
            "剩余预算字节": config["资源总预算字节"] - spent,
            "entries": entries,
            "skipped": skipped,
            "候选总数": candidates["total"],
            "入选数量": len(entries),
            "跳过数量": len(skipped),
            "同表重复任务": candidates["同表重复任务"],
        }

    def _select(self, items: list[dict[str, Any]], budget: int,
                reserved_total: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        """按给定顺序贪心选表：预算不够就跳过当前候选，继续比对剩余候选。"""
        entries: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        spent = 0
        available = max(budget - reserved_total, 0)
        for item in items:
            if not item.get("可编排"):
                skipped.append({
                    "表名称": item.get("表名称"),
                    "任务编号": item.get("任务编号"),
                    "原因": item.get("跳过原因") or "不具备编排条件",
                })
                continue
            if item.get("已预留"):
                skipped.append({
                    "表名称": item["表名称"],
                    "任务编号": item.get("任务编号"),
                    "原因": "该表已被其他执行计划预留预算，避免重复计算收益",
                })
                continue
            cost = int(item["预算占用字节"])
            if spent + cost > available:
                skipped.append({
                    "表名称": item["表名称"],
                    "任务编号": item.get("任务编号"),
                    "原因": f"预算不足，剩余 {format_size(max(available - spent, 0))}，跳过并继续比对剩余候选",
                })
                continue
            spent += cost
            entry = dict(item)
            entry.update({
                "执行结果": "待执行",
                "实际优化前容量": "",
                "实际优化前文件数": "",
                "实际优化后容量": "",
                "实际优化后文件数": "",
                "实际收益": "",
                "实际收益字节": 0,
                "执行说明": "已预留预算，等待执行",
            })
            entries.append(entry)
        return entries, skipped, spent

    def create_plan(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with _LOCK:
            config = self.get_config()
            order = str(values.get("排序") or config["默认排序"])
            if order not in SORT_OPTIONS:
                return None, f"排序方式「{order}」不在可选范围：{'、'.join(SORT_OPTIONS)}"
            candidates = self.candidates(sort_by=order)
            entries, skipped, spent = self._select(candidates["items"], config["资源总预算字节"],
                                                   candidates["预算"]["已预留字节"])
            if not entries:
                return None, "当前没有可以编排的表：预算不足或缺少容量统计，请先补齐优化任务与容量统计"
            rows = store.rows(PLAN_MODULE)
            plan_id = _next_id(rows)
            plan = {
                "id": plan_id,
                "计划编号": _number(rows, "计划编号", "PLAN"),
                "版本": 1,
                "状态": "已保存",
                "排序": order,
                "行组大小上限": config["行组大小上限"],
                "资源总预算": config["资源总预算"],
                "资源总预算字节": config["资源总预算字节"],
                "已用预算": format_size(spent),
                "已用预算字节": spent,
                "剩余预算": format_size(config["资源总预算字节"] - spent),
                "剩余预算字节": config["资源总预算字节"] - spent,
                "候选总数": candidates["total"],
                "入选数量": len(entries),
                "跳过数量": len(skipped),
                "entries": entries,
                "skipped": skipped,
                "同表重复任务": candidates["同表重复任务"],
                "创建人": str(values.get("创建人") or "值班管理员"),
                "创建时间": _now(),
                "更新时间": _now(),
                "执行时间": "",
                "恢复结论": "",
                "恢复时间": "",
                "统计报告编号": [],
            }
            rows.append(plan)
            self._rebuild_reconcile(plan)
            self._record_version(plan, "保存执行计划")
            return plan, f"执行计划 {plan['计划编号']} 已保存，版本 v{plan['版本']}"

    def list_plans(self, *, keyword: str | None = None, status: str | None = None,
                   page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = list(reversed(store.rows(PLAN_MODULE)))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if str(row.get("状态")) == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._plan_summary(row) for row in rows[start:start + size]], total

    def _plan_summary(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": plan.get("id"),
            "计划编号": plan.get("计划编号"),
            "版本": plan.get("版本"),
            "状态": plan.get("状态"),
            "排序": plan.get("排序"),
            "行组大小上限": plan.get("行组大小上限"),
            "资源总预算": plan.get("资源总预算"),
            "已用预算": plan.get("已用预算"),
            "剩余预算": plan.get("剩余预算"),
            "候选总数": plan.get("候选总数"),
            "入选数量": plan.get("入选数量"),
            "跳过数量": plan.get("跳过数量"),
            "创建人": plan.get("创建人"),
            "创建时间": plan.get("创建时间"),
            "更新时间": plan.get("更新时间"),
            "执行时间": plan.get("执行时间"),
            "恢复结论": plan.get("恢复结论"),
            "统计报告编号": plan.get("统计报告编号", []),
        }

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        plan = store.find(PLAN_MODULE, plan_id)
        if plan is None:
            return None
        detail = dict(plan)
        detail["版本历史"] = self.versions(plan_id)
        detail["预算核对清单"] = self.list_reconcile(plan_id=plan_id)
        detail["版本核对"] = {
            "计划版本": plan.get("版本"),
            "核对清单版本": sorted({
                item.get("计划版本") for item in detail["预算核对清单"]
            }),
            "一致": all(item.get("计划版本") == plan.get("版本") for item in detail["预算核对清单"]),
        }
        detail["待处理冲突"] = [
            item for item in self.list_conflicts(plan_id=plan_id) if item.get("状态") == "待处理"
        ]
        return detail

    def versions(self, plan_id: int) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(VERSION_MODULE)
            if _as_int(row.get("计划id")) == plan_id
        ]

    def _record_version(self, plan: dict[str, Any], action: str) -> None:
        rows = store.rows(VERSION_MODULE)
        rows.append({
            "id": _next_id(rows),
            "计划id": plan.get("id"),
            "计划编号": plan.get("计划编号"),
            "版本": plan.get("版本"),
            "状态": plan.get("状态"),
            "排序": plan.get("排序"),
            "已用预算": plan.get("已用预算"),
            "入选数量": plan.get("入选数量"),
            "跳过数量": plan.get("跳过数量"),
            "操作": action,
            "操作人": plan.get("创建人") or "值班管理员",
            "时间": _now(),
            "任务快照": [entry.get("任务编号") for entry in plan.get("entries", [])],
        })

    # ------------------------------------------------------------------
    # 预算核对清单（下游处理模块）
    # ------------------------------------------------------------------
    def _rebuild_reconcile(self, plan: dict[str, Any]) -> None:
        """按当前版本重建核对项：每个入选任务一条，版本与计划保持一致。

        重建会清掉这张计划所有旧版本的核对行，保证预算核对清单不会同时指向两个版本。
        """
        rows = store.rows(RECONCILE_MODULE)
        plan_id = _as_int(plan.get("id"))
        version = _as_int(plan.get("版本"))
        serial = max((_digits(row.get("核对编号")) for row in rows), default=0)
        rows[:] = [row for row in rows if _as_int(row.get("计划id")) != plan_id]
        budget = _as_int(plan.get("资源总预算字节"))
        cumulative = 0
        for entry in plan.get("entries", []):
            cost = _as_int(entry.get("预算占用字节"))
            cumulative += cost
            serial += 1
            rows.append({
                "id": _next_id(rows),
                "核对编号": f"RECON-{serial:04d}",
                "计划id": plan_id,
                "计划编号": plan.get("计划编号"),
                "计划版本": version,
                "表名称": entry.get("表名称"),
                "任务编号": entry.get("任务编号"),
                "优化类型": entry.get("优化类型"),
                "预算占用": entry.get("预算占用"),
                "预算占用字节": cost,
                "累计预算": format_size(cumulative),
                "累计预算字节": cumulative,
                "预算上限": plan.get("资源总预算"),
                "预算上限字节": budget,
                "预算结论": "预算内" if cumulative <= budget else "超预算",
                "优化前容量": entry.get("优化前容量"),
                "优化前文件数": entry.get("优化前文件数"),
                "优化后容量": entry.get("优化后容量"),
                "优化后文件数": entry.get("优化后文件数"),
                "预计收益": entry.get("预计收益"),
                "实际收益": entry.get("实际收益") or "—",
                "实际收益字节": entry.get("实际收益字节") or 0,
                "核对状态": "待执行",
                "核对说明": f"已按 v{version} 预留预算，等待执行后核对实际效果",
                "恢复结论": "",
                "更新时间": _now(),
            })

    def sync_reconcile(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """读取执行计划，把实际执行结果同步进预算核对清单。"""
        rows = [
            row for row in store.rows(RECONCILE_MODULE)
            if _as_int(row.get("计划id")) == _as_int(plan.get("id"))
            and _as_int(row.get("计划版本")) == _as_int(plan.get("版本"))
        ]
        entry_map = {str(entry.get("任务编号")): entry for entry in plan.get("entries", [])}
        for row in rows:
            entry = entry_map.get(str(row.get("任务编号")))
            if entry is None:
                row["核对状态"] = "已失效"
                row["核对说明"] = "该任务不在当前版本的执行计划里，核对项已失效"
                row["更新时间"] = _now()
                continue
            if entry.get("执行结果") != "已执行":
                row["核对状态"] = "待执行"
                row["核对说明"] = f"已按 v{plan.get('版本')} 预留预算，等待执行后核对实际效果"
                row["更新时间"] = _now()
                continue
            actual = _as_int(entry.get("实际收益字节"))
            expected = _as_int(entry.get("预计收益字节"))
            deviation = (actual - expected) / expected if expected else 0.0
            row["实际收益"] = entry.get("实际收益") or format_size(actual)
            row["实际收益字节"] = actual
            row["优化前容量"] = entry.get("实际优化前容量") or entry.get("优化前容量")
            row["优化前文件数"] = entry.get("实际优化前文件数") or entry.get("优化前文件数")
            row["优化后容量"] = entry.get("实际优化后容量") or entry.get("优化后容量")
            row["优化后文件数"] = entry.get("实际优化后文件数") or entry.get("优化后文件数")
            row["核对状态"] = "已核对" if abs(deviation) <= DEVIATION_LIMIT else "有差异"
            row["核对说明"] = (
                f"实际收益 {format_size(actual)}，预计 {format_size(expected)}，"
                f"偏差 {format_percent(deviation)}，计划版本 v{plan.get('版本')}"
            )
            row["更新时间"] = _now()
        return rows

    def list_reconcile(self, *, plan_id: int | None = None, version: int | None = None,
                       page: int = 1, size: int = 200) -> list[dict[str, Any]]:
        rows = list(store.rows(RECONCILE_MODULE))
        if plan_id is not None:
            rows = [row for row in rows if _as_int(row.get("计划id")) == plan_id]
        if version is not None:
            rows = [row for row in rows if _as_int(row.get("计划版本")) == version]
        start = max(page - 1, 0) * size
        return rows[start:start + size]

    # ------------------------------------------------------------------
    # 版本冲突
    # ------------------------------------------------------------------
    def list_conflicts(self, *, plan_id: int | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(CONFLICT_MODULE))
        if plan_id is not None:
            rows = [row for row in rows if _as_int(row.get("计划id")) == plan_id]
        return rows

    def detect_conflicts(self, plan_id: int | None = None) -> list[dict[str, Any]]:
        """新到达的高收益任务与已预留预算冲突：登记冲突并把计划标成版本冲突。"""
        with _LOCK:
            stats_map = self._latest_stats()
            group = self.get_config()["行组大小上限字节"]
            created: list[dict[str, Any]] = []
            plans = [
                plan for plan in store.rows(PLAN_MODULE)
                if str(plan.get("状态")) in ACTIVE_PLAN_STATUSES
                and (plan_id is None or _as_int(plan.get("id")) == plan_id)
            ]
            for plan in plans:
                selected = {str(entry.get("表名称")): entry for entry in plan.get("entries", [])}
                selected_tasks = {str(entry.get("任务编号")) for entry in plan.get("entries", [])}
                for table, entry in selected.items():
                    stats = stats_map.get(table)
                    if stats is None:
                        continue
                    for task in store.rows(TASK_MODULE):
                        if str(task.get("表名称")) != table:
                            continue
                        if str(task.get("任务编号")) in selected_tasks:
                            continue
                        if str(task.get("status")) not in ACTIVE_TASK_STATUSES:
                            continue
                        estimate = self._estimate(task, stats, group)
                        if not estimate.get("可编排"):
                            continue
                        if estimate["收益比例值"] <= _as_float(entry.get("收益比例值")):
                            continue
                        if self._conflict_exists(plan, task):
                            continue
                        rows = store.rows(CONFLICT_MODULE)
                        conflict = {
                            "id": _next_id(rows),
                            "冲突编号": _number(rows, "冲突编号", "CONF"),
                            "计划id": plan.get("id"),
                            "计划编号": plan.get("计划编号"),
                            "计划版本": plan.get("版本"),
                            "表名称": table,
                            "原任务编号": entry.get("任务编号"),
                            "原收益比例": entry.get("收益比例"),
                            "新任务编号": task.get("任务编号"),
                            "新收益比例": estimate.get("收益比例"),
                            "新任务预计收益": estimate.get("预计收益"),
                            "新任务预算占用": estimate.get("预算占用"),
                            "状态": "待处理",
                            "处理方式": "",
                            "说明": (
                                f"新任务 {task.get('任务编号')} 收益比例 {estimate.get('收益比例')} "
                                f"高于已预留任务 {entry.get('任务编号')} 的 {entry.get('收益比例')}"
                            ),
                            "创建时间": _now(),
                            "处理时间": "",
                        }
                        rows.append(conflict)
                        created.append(conflict)
                        plan["状态"] = "版本冲突"
                        plan["更新时间"] = _now()
            return created

    def _conflict_exists(self, plan: dict[str, Any], task: dict[str, Any]) -> bool:
        for row in store.rows(CONFLICT_MODULE):
            if (_as_int(row.get("计划id")) == _as_int(plan.get("id"))
                    and str(row.get("新任务编号")) == str(task.get("任务编号"))):
                return True
        return False

    def resolve_conflict(self, conflict_id: int, action: str,
                         remark: str | None = None) -> tuple[dict[str, Any] | None, str]:
        with _LOCK:
            rows = store.rows(CONFLICT_MODULE)
            conflict = None
            for row in rows:
                if _as_int(row.get("id")) == conflict_id:
                    conflict = row
                    break
            if conflict is None:
                return None, f"版本冲突 {conflict_id} 不存在"
            if str(conflict.get("状态")) != "待处理":
                return None, f"版本冲突 {conflict.get('冲突编号')} 已处理，无需重复操作"
            if action not in {"保留原任务", "改排新任务"}:
                return None, f"动作「{action}」不在版本冲突处理范围：保留原任务、改排新任务"
            plan = store.find(PLAN_MODULE, _as_int(conflict.get("计划id")))
            if plan is None:
                return None, "冲突对应的执行计划不存在"
            if str(plan.get("状态")) not in ACTIVE_PLAN_STATUSES:
                return None, f"执行计划当前状态为「{plan.get('状态')}」，不能再调整版本"

            if action == "保留原任务":
                conflict["状态"] = "保留原任务"
                conflict["处理方式"] = "保留原任务"
                conflict["处理时间"] = _now()
                conflict["说明"] = remark or "保留原任务，新任务不占用本轮预算"
                plan["skipped"].append({
                    "表名称": conflict.get("表名称"),
                    "任务编号": conflict.get("新任务编号"),
                    "原因": f"版本冲突处理：保留原任务 {conflict.get('原任务编号')}",
                })
                plan["跳过数量"] = len(plan["skipped"])
            else:
                entry = next(
                    (item for item in plan.get("entries", [])
                     if str(item.get("任务编号")) == str(conflict.get("原任务编号"))),
                    None,
                )
                if entry is None:
                    return None, "计划里找不到被替换的原任务，无法改排"
                stats = self._latest_stats().get(str(conflict.get("表名称")))
                task = next(
                    (item for item in store.rows(TASK_MODULE)
                     if str(item.get("任务编号")) == str(conflict.get("新任务编号"))),
                    None,
                )
                if stats is None or task is None:
                    return None, "新任务或容量统计已失效，无法改排"
                estimate = self._estimate(task, stats, self.get_config()["行组大小上限字节"])
                if not estimate.get("可编排"):
                    return None, f"新任务不可编排：{estimate.get('跳过原因')}"
                used_without = _as_int(plan.get("已用预算字节")) - _as_int(entry.get("预算占用字节"))
                if used_without + _as_int(estimate.get("预算占用字节")) > _as_int(plan.get("资源总预算字节")):
                    return None, (
                        f"改排新任务后预算将超过 {plan.get('资源总预算')}，"
                        "请先调整预算或保留原任务"
                    )
                replacement = dict(estimate)
                replacement.update({
                    "执行结果": "待执行",
                    "实际优化前容量": "",
                    "实际优化前文件数": "",
                    "实际优化后容量": "",
                    "实际优化后文件数": "",
                    "实际收益": "",
                    "实际收益字节": 0,
                    "执行说明": "版本冲突后改排，已预留预算",
                })
                index = plan["entries"].index(entry)
                plan["entries"][index] = replacement
                plan["已用预算字节"] = used_without + _as_int(replacement.get("预算占用字节"))
                plan["已用预算"] = format_size(plan["已用预算字节"])
                plan["剩余预算字节"] = _as_int(plan.get("资源总预算字节")) - plan["已用预算字节"]
                plan["剩余预算"] = format_size(max(plan["剩余预算字节"], 0))
                plan["skipped"].append({
                    "表名称": conflict.get("表名称"),
                    "任务编号": conflict.get("原任务编号"),
                    "原因": f"版本冲突处理：改排收益更高的任务 {conflict.get('新任务编号')}",
                })
                plan["跳过数量"] = len(plan["skipped"])
                conflict["状态"] = "改排新任务"
                conflict["处理方式"] = "改排新任务"
                conflict["处理时间"] = _now()
                conflict["说明"] = remark or "改排收益更高的新任务，原任务退回待执行"

            plan["版本"] = _as_int(plan.get("版本"), 1) + 1
            plan["状态"] = "已保存"
            plan["更新时间"] = _now()
            self._rebuild_reconcile(plan)
            self._record_version(plan, action)
            return plan, (
                f"版本冲突 {conflict.get('冲突编号')} 已按「{action}」处理，"
                f"执行计划升到 v{plan['版本']}，预算核对清单同步到同版本"
            )

    # ------------------------------------------------------------------
    # 计划动作
    # ------------------------------------------------------------------
    def run_action(self, plan_id: int, action: str,
                   remark: str | None = None) -> tuple[dict[str, Any] | None, str]:
        with _LOCK:
            plan = store.find(PLAN_MODULE, plan_id)
            if plan is None:
                return None, f"执行计划 {plan_id} 不存在或已归档"
            pending = [item for item in store.rows(CONFLICT_MODULE)
                       if _as_int(item.get("计划id")) == plan_id and item.get("状态") == "待处理"]
            if action not in {"执行计划", "重新编排", "同步核对", "恢复", "归档计划"}:
                return None, f"动作「{action}」不属于执行计划可执行范围"
            if action in {"执行计划", "重新编排"} and pending:
                return None, (
                    f"执行计划存在 {len(pending)} 条待处理的版本冲突，"
                    "请先选择保留原任务或改排新任务"
                )
            if action == "执行计划":
                return self._execute(plan)
            if action == "重新编排":
                return self._replan(plan, remark)
            if action == "同步核对":
                self.sync_reconcile(plan)
                return plan, f"执行计划 {plan.get('计划编号')} 的核对结果已同步到预算核对清单"
            if action == "恢复":
                return self._restore(plan, remark)
            plan["状态"] = "已归档"
            plan["更新时间"] = _now()
            self._record_version(plan, "归档计划")
            return plan, f"执行计划 {plan.get('计划编号')} 已归档"

    def _execute(self, plan: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if str(plan.get("状态")) not in {"已保存", "执行中"}:
            return None, f"执行计划当前状态为「{plan.get('状态')}」，不能再次执行"
        plan["状态"] = "执行中"
        plan["更新时间"] = _now()
        stats_map = self._latest_stats()
        executed = 0
        for entry in plan.get("entries", []):
            if entry.get("执行结果") == "已执行":
                continue
            # 实际效果按执行台账重算：合并不改变数据本身，收益约为预计值的 96%；
            # 压缩要整表重写，落地后收益约为预计值的 90%。
            factor = 0.90 if entry.get("优化类型") == "数据压缩" else 0.96
            expected = _as_int(entry.get("预计收益字节"))
            before_size = _as_int(entry.get("优化前容量字节"))
            before_files = _as_int(entry.get("优化前文件数"))
            actual = int(expected * factor)
            after_size = max(before_size - actual, 0)
            after_files = _as_int(entry.get("优化后文件数"))
            entry.update({
                "执行结果": "已执行",
                "实际优化前容量": format_size(before_size),
                "实际优化前文件数": before_files,
                "实际优化后容量": format_size(after_size),
                "实际优化后文件数": after_files,
                "实际收益": format_size(actual),
                "实际收益字节": actual,
                "执行说明": f"按 v{plan.get('版本')} 批次执行完成，仅记录被选计划的实际效果",
            })
            self._finish_task(plan, entry)
            report = self._write_stats(plan, entry, stats_map)
            if report and report.get("报告编号") not in plan["统计报告编号"]:
                plan["统计报告编号"].append(report.get("报告编号"))
            executed += 1
        plan["状态"] = "已完成"
        plan["执行时间"] = _now()
        plan["更新时间"] = _now()
        self.sync_reconcile(plan)
        self._record_version(plan, "执行计划")
        return plan, (
            f"执行计划 {plan.get('计划编号')} v{plan.get('版本')} 已执行，"
            f"{executed} 张表的优化效果已写入表统计信息，预算核对清单已同步"
        )

    def _finish_task(self, plan: dict[str, Any], entry: dict[str, Any]) -> None:
        for task in store.rows(TASK_MODULE):
            if str(task.get("任务编号")) != str(entry.get("任务编号")):
                continue
            task["status"] = "已完成"
            task["pending"] = False
            task["执行计划"] = f"{plan.get('计划编号')} v{plan.get('版本')}"
            task["资源消耗"] = entry.get("预算占用")
            task["优化效果"] = (
                f"文件数 {entry.get('实际优化前文件数')} → {entry.get('实际优化后文件数')}，"
                f"容量 {entry.get('实际优化前容量')} → {entry.get('实际优化后容量')}，"
                f"收益 {entry.get('实际收益')}"
            )
            return

    def _write_stats(self, plan: dict[str, Any], entry: dict[str, Any],
                     stats_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """把执行后的实际文件数与容量写回表统计信息，并挂上计划版本。"""
        table = str(entry.get("表名称"))
        rows = store.rows(STATS_MODULE)
        report = None
        current = stats_map.get(table)
        if current is not None:
            for row in rows:
                if str(row.get("报告编号")) == str(current.get("报告编号")):
                    report = row
                    break
        if report is None:
            report = {
                "id": _next_id(rows),
                "报告编号": _number(rows, "报告编号", "STAT"),
                "表名称": table,
                "行总数": 0,
                "列统计": "执行计划回写生成",
                "统计时间": _now(),
                "pending": False,
                "abnormal": False,
            }
            rows.append(report)
        report["文件总数"] = _as_int(entry.get("实际优化后文件数"))
        report["总容量"] = entry.get("实际优化后容量")
        report["status"] = "已采集"
        report["报告状态"] = "已采集"
        report["pending"] = False
        report["统计时间"] = str(_now())[:10]
        report["计划编号"] = plan.get("计划编号")
        report["计划版本"] = plan.get("版本")
        report["优化前文件数"] = entry.get("实际优化前文件数")
        report["优化前容量"] = entry.get("实际优化前容量")
        report["优化后文件数"] = entry.get("实际优化后文件数")
        report["优化后容量"] = entry.get("实际优化后容量")
        report["优化收益"] = entry.get("实际收益")
        report["优化效果"] = (
            f"文件数 {entry.get('实际优化前文件数')} → {entry.get('实际优化后文件数')}，"
            f"容量 {entry.get('实际优化前容量')} → {entry.get('实际优化后容量')}，"
            f"实际收益 {entry.get('实际收益')}（{plan.get('计划编号')} v{plan.get('版本')}）"
        )
        return report

    def _replan(self, plan: dict[str, Any], remark: str | None) -> tuple[dict[str, Any] | None, str]:
        if str(plan.get("状态")) not in {"已保存", "版本冲突", "已完成", "已恢复"}:
            return None, f"执行计划当前状态为「{plan.get('状态')}」，不能重新编排"
        config = self.get_config()
        order = str(remark or plan.get("排序") or config["默认排序"])
        if order not in SORT_OPTIONS:
            order = str(plan.get("排序") or config["默认排序"])
        candidates = self.candidates(sort_by=order, exclude_plan_id=_as_int(plan.get("id")))
        entries, skipped, spent = self._select(candidates["items"], config["资源总预算字节"],
                                               candidates["预算"]["已预留字节"])
        if not entries:
            return None, "剩余候选都放不进预算，本次重新编排没有可执行批次"
        plan["entries"] = entries
        plan["skipped"] = skipped
        plan["排序"] = order
        plan["行组大小上限"] = config["行组大小上限"]
        plan["资源总预算"] = config["资源总预算"]
        plan["资源总预算字节"] = config["资源总预算字节"]
        plan["已用预算字节"] = spent
        plan["已用预算"] = format_size(spent)
        plan["剩余预算字节"] = config["资源总预算字节"] - spent
        plan["剩余预算"] = format_size(max(config["资源总预算字节"] - spent, 0))
        plan["候选总数"] = candidates["total"]
        plan["入选数量"] = len(entries)
        plan["跳过数量"] = len(skipped)
        plan["同表重复任务"] = candidates["同表重复任务"]
        plan["统计报告编号"] = []
        plan["执行时间"] = ""
        plan["状态"] = "已保存"
        plan["版本"] = _as_int(plan.get("版本"), 1) + 1
        plan["更新时间"] = _now()
        plan["恢复结论"] = ""
        plan["恢复时间"] = ""
        self._rebuild_reconcile(plan)
        self._record_version(plan, "重新编排")
        return plan, f"执行计划已按「{order}」重新编排，升级到 v{plan['版本']}"

    def _restore(self, plan: dict[str, Any], remark: str | None) -> tuple[dict[str, Any] | None, str]:
        if str(plan.get("状态")) not in {"已完成", "已恢复"}:
            return None, f"执行计划当前状态为「{plan.get('状态')}」，只有已执行的计划可以记录恢复结论"
        conclusion = str(remark or "恢复完成，优化效果保留在表统计信息中，预算核对清单已记录恢复结论")
        plan["状态"] = "已恢复"
        plan["恢复结论"] = conclusion
        plan["恢复时间"] = _now()
        plan["更新时间"] = _now()
        self.sync_reconcile(plan)
        for row in self.list_reconcile(plan_id=_as_int(plan.get("id")), version=_as_int(plan.get("版本"))):
            row["核对状态"] = "已恢复"
            row["恢复结论"] = conclusion
            row["核对说明"] = f"{row.get('核对说明')}；恢复结论：{conclusion}"
            row["更新时间"] = _now()
        self._record_version(plan, "恢复")
        return plan, f"恢复结论已记入预算核对清单（{plan.get('计划编号')} v{plan.get('版本')}）"

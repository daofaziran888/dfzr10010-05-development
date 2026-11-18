"""表优化管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "iceberg"
REQUIRED_FIELDS = ["任务编号", "表名称", "优化类型"]
STATUS_ORDER = ["待执行", "执行中", "已完成", "已取消"]
ACTION_RULES = {"执行优化": "执行中", "取消优化": "已取消", "归档任务": "已完成"}
NEGATIVE_ACTIONS = []
# 同一张表同时只允许一个活动任务；否则容量统计会把同一份收益重复计算。
ACTIVE_STATUSES = {"待执行", "执行中"}


class IcebergService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        detail = dict(entry)
        plans = [
            plan for plan in store.rows("_iceberg_plans")
            if any(str(item.get("任务编号")) == str(entry.get("任务编号"))
                   for item in plan.get("entries", []))
        ]
        detail["执行计划批次"] = [
            {
                "计划id": plan.get("id"),
                "计划编号": plan.get("计划编号"),
                "版本": plan.get("版本"),
                "状态": plan.get("状态"),
                "资源总预算": plan.get("资源总预算"),
                "已用预算": plan.get("已用预算"),
            }
            for plan in plans
        ]
        return detail

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        table = str(values.get("表名称")).strip()
        conflict = self._active_task(table)
        if conflict is not None:
            return None, [
                f"表 {table} 已有活动优化任务 {conflict.get('任务编号')}，"
                "同表只允许一个活动优化任务，避免容量统计重复计算收益"
            ]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["表名称"] = table
        entry.update({
            "优化参数": values.get("优化参数") or "",
            "执行计划": values.get("执行计划") or "",
            "资源消耗": values.get("资源消耗") or "",
            "优化效果": values.get("优化效果") or "",
        })
        entry["status"] = STATUS_ORDER[0]
        entry["任务状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def _active_task(self, table: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("表名称")) != table:
                continue
            if str(row.get("status")) in ACTIVE_STATUSES:
                return row
        return None

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"优化任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表优化管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["任务状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"优化任务已{action}"

"""表优化执行计划接口：候选编排、预算、核对清单与版本冲突处理。

路由只做参数收敛，预算与版本规则都在 services/plan.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.plan import PLAN_STATUSES, SORT_OPTIONS, IcebergPlanService

router = APIRouter(prefix="/api/plan", tags=["表优化执行计划"])

service = IcebergPlanService()


@router.get("/config", response_model=dict)
def get_config() -> dict:
    """读取执行计划的资源总预算、行组大小上限与默认排序。"""
    return service.get_config()


@router.post("/config", response_model=ActionResult)
def update_config(payload: EntryPayload) -> ActionResult:
    """调整资源总预算/行组大小上限；预算必须是正数，否则拦下并说明原因。"""
    config, message = service.update_config(payload.values)
    if config is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=config)


@router.get("/capacity", response_model=dict)
def capacity() -> dict:
    """容量统计视图：读取统计信息，按表给出容量、文件数与建议文件数。"""
    items = service.capacity_stats()
    return {"module": "plan/capacity", "total": len(items), "items": items}


@router.get("/candidates", response_model=dict)
def candidates(
    sort_by: str | None = Query(default=None, description="、".join(SORT_OPTIONS)),
    status: str | None = Query(default=None, description="待执行、执行中"),
) -> dict:
    """执行计划候选视图：任务、容量、预算与预计收益同一行，支持多种排序。"""
    if sort_by and sort_by not in SORT_OPTIONS:
        raise HTTPException(status_code=400, detail=f"排序方式不在可选范围：{'、'.join(SORT_OPTIONS)}")
    return service.candidates(sort_by=sort_by, status=status)


@router.post("/preview", response_model=dict)
def preview_plan(payload: EntryPayload) -> dict:
    """试算执行计划：不落库，只返回候选、预算与收益。"""
    return service.plan_preview(payload.values)


@router.get("/plans", response_model=PageResult[dict])
def list_plans(
    keyword: str | None = Query(default=None, description="按计划编号检索"),
    status: str | None = Query(default=None, description="、".join(PLAN_STATUSES)),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """执行计划列表：按计划编号与状态过滤，新计划排在前面。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_plans(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/plans", response_model=ActionResult)
def create_plan(payload: EntryPayload) -> ActionResult:
    """按当前候选生成并保存执行计划；预算不足时从剩余候选继续。"""
    plan, message = service.create_plan(payload.values)
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=plan)


@router.get("/plans/{plan_id}", response_model=dict)
def get_plan(plan_id: int) -> dict:
    """执行计划明细：含每张表预算、优化前后文件与容量、实际效果与核对清单。"""
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"执行计划 {plan_id} 不存在或已归档")
    return plan


@router.post("/plans/{plan_id}/actions", response_model=ActionResult)
def run_action(plan_id: int, payload: EntryPayload) -> ActionResult:
    """对执行计划执行执行计划、重新编排、同步核对、恢复、归档；不合法动作说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    remark = payload.values.get("remark") or payload.remark
    plan, message = service.run_action(plan_id, action, str(remark) if remark else None)
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=plan)


@router.get("/plans/{plan_id}/reconcile", response_model=dict)
def plan_reconcile(plan_id: int, version: int | None = Query(default=None)) -> dict:
    """读取执行计划对应版本的预算核对清单。"""
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"执行计划 {plan_id} 不存在或已归档")
    items = service.list_reconcile(plan_id=plan_id, version=version)
    return {
        "计划编号": plan.get("计划编号"),
        "计划版本": plan.get("版本"),
        "total": len(items),
        "items": items,
    }


@router.get("/plans/{plan_id}/versions", response_model=dict)
def plan_versions(plan_id: int) -> dict:
    """执行计划的版本历史，用于核对统计信息与预算清单是否同版本。"""
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"执行计划 {plan_id} 不存在或已归档")
    items = service.versions(plan_id)
    return {"计划编号": plan.get("计划编号"), "total": len(items), "items": items}


@router.get("/reconcile", response_model=dict)
def reconcile(plan_id: int | None = Query(default=None), version: int | None = Query(default=None)) -> dict:
    """预算核对清单总览：可按计划与版本过滤。"""
    items = service.list_reconcile(plan_id=plan_id, version=version)
    return {"module": "plan/reconcile", "total": len(items), "items": items}


@router.get("/conflicts", response_model=dict)
def list_conflicts(plan_id: int | None = Query(default=None)) -> dict:
    """版本冲突列表：新到达的高收益任务与已预留预算冲突。"""
    items = service.list_conflicts(plan_id=plan_id)
    return {"total": len(items), "items": items}


@router.post("/conflicts/detect", response_model=ActionResult)
def detect_conflicts(payload: EntryPayload) -> ActionResult:
    """扫描新任务与已预留预算的版本冲突；发现后计划标记为版本冲突。"""
    plan_id = payload.values.get("plan_id")
    try:
        target = int(plan_id) if plan_id not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="plan_id 需要是整数")
    created = service.detect_conflicts(target)
    if not created:
        return ActionResult(ok=True, message="没有发现新的版本冲突")
    return ActionResult(
        ok=True,
        message=f"发现 {len(created)} 条版本冲突，请选择保留原任务或改排新任务",
        entry={"items": created},
    )


@router.post("/conflicts/{conflict_id}/actions", response_model=ActionResult)
def resolve_conflict(conflict_id: int, payload: EntryPayload) -> ActionResult:
    """处理版本冲突：保留原任务或改排新任务，都会推进计划版本并同步核对清单。"""
    action = str(payload.values.get("action") or "").strip()
    remark = payload.values.get("remark") or payload.remark
    plan, message = service.resolve_conflict(conflict_id, action, str(remark) if remark else None)
    if plan is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=plan)


@router.get("/export")
def export_plans() -> dict[str, Any]:
    """导出执行计划清单（不含明细），方便和预算核对清单一起归档。"""
    items, total = service.list_plans(page=1, size=10000)
    return {"module": "plan", "total": total, "items": items}

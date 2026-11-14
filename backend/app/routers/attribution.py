"""归因模型接口：维护归因规则，覆盖提交规则评审、发布规则、下线规则与规则升版。

权限口径：analyst 只读；reviewer 可登记/升版/提交评审；发布与下线仅 release_manager，
且业务归属必须匹配。已发布规则版本冻结，改动只能走升版。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.attribution import AttributionService
from app.services.auth import Actor, get_actor, require_edit_draft, require_release

router = APIRouter(prefix="/api/attribution", tags=["归因模型"])

service = AttributionService()

LIST_FIELDS = ["规则编号", "规则版本", "归因类型", "回溯窗口天", "触点权重", "首次触点权重", "末次触点权重", "中间触点衰减", "关联渠道", "业务归属"]
META_FIELDS = ["id", "status", "pending", "abnormal"]
STATUSES = ["草稿", "规则评审中", "已发布", "已下线"]
RELEASE_ACTIONS = {"发布规则", "下线规则"}


def _project(row: dict[str, Any]) -> dict[str, Any]:
    """列表只暴露与详情对齐的字段，避免复核入口字段错位。"""
    return {field: row.get(field) for field in [*META_FIELDS, *LIST_FIELDS]}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤归因模型列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=[_project(row) for row in items], total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出归因模型清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "attribution", "total": total, "items": [_project(row) for row in items]}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条归因规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"归因规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """登记一条归因规则，缺字段时说明原因而不是静默丢弃。"""
    require_edit_draft(actor, "归因规则")
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="归因规则已登记", entry=entry)


@router.post("/{entry_id}/upgrade", response_model=ActionResult)
def upgrade_entry(entry_id: int, payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """规则升版：基于已发布版本生成新版本，旧版本冻结保留，历史契约不受影响。"""
    require_edit_draft(actor, "归因规则")
    entry, message = service.upgrade_rule(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """对单条归因规则执行提交规则评审、发布规则、下线规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry = service.get_entry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"归因规则 {entry_id} 不存在或已归档")
    if action in RELEASE_ACTIONS:
        require_release(actor, entry.get("业务归属"), action)
    else:
        require_edit_draft(actor, "归因规则")
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

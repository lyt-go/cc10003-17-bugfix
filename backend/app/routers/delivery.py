"""数据服务接口：发布冻结契约、版本切换/回滚、消费方读取链路。

权限口径：analyst 只读；reviewer 可登记/提交评审；发布、切换、回滚、下线、
调整消费方状态仅 release_manager，且业务归属必须匹配。
读取链路（serve）对授权消费方开放；消费方停止或规则下线时返回明确不可用原因。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.auth import Actor, get_actor, require_edit_draft, require_release
from app.services.delivery import DeliveryService

router = APIRouter(prefix="/api/delivery", tags=["数据服务"])

service = DeliveryService()

LIST_FIELDS = ["服务编号", "数据集名称", "规则编号", "版本号", "返回字段", "查询参数定义", "速率限制", "分页最大条数", "消费方应用", "业务归属"]
META_FIELDS = ["id", "status", "pending", "abnormal", "is_active"]
STATUSES = ["草稿", "服务评审中", "已发布", "已下线"]
RELEASE_ACTIONS = {"发布服务", "下线服务"}


def _project(row: dict[str, Any]) -> dict[str, Any]:
    """列表字段与详情/契约对齐，复核入口不再错位。"""
    projected = {field: row.get(field) for field in [*META_FIELDS, *LIST_FIELDS]}
    contract = row.get("契约") or {}
    projected["规则版本"] = contract.get("规则版本")
    return projected


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按服务编号检索"),
    status: str | None = Query(default=None, description="草稿、服务评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按服务编号与状态过滤数据服务列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=[_project(row) for row in items], total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据服务清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "delivery", "total": total, "items": [_project(row) for row in items]}


@router.get("/{service_no}/serve")
def serve_entry(
    service_no: str,
    consumer_app: str = Query(description="消费方应用标识"),
    version_no: int | None = Query(default=None, description="指定历史服务版本；缺省取当前生效版本"),
) -> dict[str, Any]:
    """消费方读取归因结果：响应携带规则版本与服务版本号，可一路追回溯到冻结契约。

    消费方停止、规则下线、服务下线时返回明确不可用原因，绝不回退输出上一轮归因值。
    """
    payload, error = service.serve(service_no=service_no, consumer_app=consumer_app, version_no=version_no)
    if error:
        raise HTTPException(
            status_code=int(error["status_code"]),
            detail={"reason": error["reason"], "message": error["message"], "服务编号": service_no},
        )
    return payload


@router.get("/{service_no}/releases")
def release_entries(service_no: str) -> dict[str, Any]:
    """服务发布记录：每次发布/切换/回滚留痕，归因结果可按此追溯到规则版本。"""
    records = service.release_log(service_no)
    return {"服务编号": service_no, "total": len(records), "items": records}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条服务版本明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"服务版本 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """登记一条服务版本，缺字段时说明原因而不是静默丢弃。"""
    require_edit_draft(actor, "数据服务")
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="服务版本已登记", entry=entry)


@router.post("/publish", response_model=ActionResult)
def publish_entry(payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """发布服务版本：冻结归因规则、触点权重与返回字段。

    规则升版后再次发布只会生成新的服务版本，旧版本契约保留；重复发布内容一致时
    幂等返回现有契约；任一契约字段无效时整次回退。
    """
    service_no = str(payload.values.get("服务编号") or "").strip()
    if not service_no:
        raise HTTPException(status_code=400, detail="缺少服务编号，无法发布")
    existing_biz = service.biz_line_of(service_no)
    requested_biz = str(payload.values.get("业务归属") or "").strip() or None
    if existing_biz and requested_biz and requested_biz != existing_biz:
        raise HTTPException(
            status_code=409,
            detail=f"服务 {service_no} 业务归属为「{existing_biz}」，发布不能改挂到「{requested_biz}」",
        )
    require_release(actor, existing_biz or requested_biz, "发布服务")
    entry, message, error_kind = service.publish(
        service_no=service_no, actor_name=actor.name, overrides=payload.values
    )
    if entry is None:
        raise HTTPException(status_code=400 if error_kind == "validation" else 409, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{service_no}/switch", response_model=ActionResult)
def switch_entry(service_no: str, payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """切换同一服务编号的生效版本；并发切换在锁内串行，任一时刻只有一份生效契约。"""
    require_release(actor, service.biz_line_of(service_no), "切换版本")
    try:
        target_version = int(payload.values.get("版本号"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="缺少目标版本号，无法切换")
    entry, message = service.switch_version(
        service_no=service_no, target_version=target_version, actor_name=actor.name
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{service_no}/rollback", response_model=ActionResult)
def rollback_entry(service_no: str, payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """回滚到历史版本：归因结果与当时的规则版本重新对齐。"""
    require_release(actor, service.biz_line_of(service_no), "回滚")
    raw = payload.values.get("版本号")
    target_version = None
    if raw not in (None, ""):
        try:
            target_version = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="目标版本号必须是整数")
    entry, message = service.rollback(service_no=service_no, actor_name=actor.name, target_version=target_version)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{service_no}/consumer", response_model=ActionResult)
def consumer_entry(service_no: str, payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """停止或恢复消费方：停止后 serve 返回明确不可用原因，不再输出上一轮归因值。"""
    require_release(actor, service.biz_line_of(service_no), "调整消费方状态")
    consumer_app = str(payload.values.get("消费方应用") or "").strip()
    status = str(payload.values.get("状态") or "").strip()
    if not consumer_app:
        raise HTTPException(status_code=400, detail="缺少消费方应用标识")
    if status not in {"正常", "已停止"}:
        raise HTTPException(status_code=400, detail="状态只支持 正常 / 已停止")
    row, message = service.set_consumer_status(service_no=service_no, consumer_app=consumer_app, status=status)
    return ActionResult(ok=True, message=message, entry=row)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload, actor: Actor = Depends(get_actor)) -> ActionResult:
    """对单条服务版本执行提交服务评审、发布服务、下线服务；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry = service.get_entry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"服务版本 {entry_id} 不存在或已归档")
    if action in RELEASE_ACTIONS:
        require_release(actor, entry.get("业务归属"), action)
    else:
        require_edit_draft(actor, "数据服务")
    entry, message = service.run_action(entry_id, action, actor_name=actor.name)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

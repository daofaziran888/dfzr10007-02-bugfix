"""数据集市构建接口：维护集市定义，覆盖发布集市、废弃集市、重建集市等动作。

固定路径（/export、/snapshots）必须声明在 /{entry_id} 之前，
否则会被动态路径抢先匹配，导出入口会拿到 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.mart import MartService

router = APIRouter(prefix="/api/mart", tags=["数据集市构建"])

service = MartService()

LIST_FIELDS = ["集市编号", "集市名称", "数据源表", "字段选择", "聚合粒度", "刷新策略", "分区字段", "集市状态"]
STATUSES = ["草稿", "构建中", "已发布", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按集市编号检索"),
    status: str | None = Query(default=None, description="草稿、构建中、已发布、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按集市编号与状态过滤数据集市构建列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据集市构建清单：与列表页使用同一份字段选择、刷新策略与状态。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "mart", "total": total, "items": items}


@router.get("/snapshots")
def list_snapshots() -> dict[str, Any]:
    """发布结果快照（导出文件）清单：重复发布不会生成重复快照。"""
    items, total = service.list_snapshots()
    return {"module": "mart", "total": total, "items": items}


@router.get("/snapshots/{version}")
def get_snapshot(version: str) -> dict[str, Any]:
    """按版本读取一份发布快照，用于核对导出文件里的字段选择与刷新策略。"""
    snapshot = service.get_snapshot(version)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"发布快照 {version} 不存在")
    return snapshot


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: str) -> dict:
    """读取单条集市定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"集市定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条集市定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.resolved_values())
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="集市定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: str, payload: EntryPayload) -> ActionResult:
    """对单条集市定义执行构建、发布、废弃、重建。

    越级动作、已发布倒回构建中、已废弃恢复都会被拦下；
    重复发布沿用第一次发布结果；失败时清单与导出文件一起回退。
    """
    values = payload.resolved_values()
    action = str(values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)

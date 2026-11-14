"""分析导出中心业务规则：状态流转、字段校验与筛选口径都收在这里。

导出任务以「数据源名称」关联集市编号。列表读取时以集市定义为准刷新
字段选择、刷新策略、分区字段与任务状态，保证列表页与导出入口看到同一份口径；
查不到对应集市时（例如历史遗留导出任务）保留任务自身的原值。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "export"
MART_MODULE = "mart"
MART_CODE_FIELD = "集市编号"
REQUIRED_FIELDS = ["任务编号", "数据源名称", "导出格式"]
# 导出任务状态与集市状态一一对应，不允许导出中心自己越过集市状态。
STATUS_ORDER = ["草稿", "导出中", "已完成", "已过期"]
ACTION_RULES = {
    "开始导出": ("草稿", "导出中"),
    "标记完成": ("导出中", "已完成"),
    "清理文件": ("已完成", "已过期"),
}
NEGATIVE_ACTIONS = ["清理文件"]
MART_STATUS_BY_EXPORT = {
    "草稿": "草稿",
    "导出中": "构建中",
    "已完成": "已发布",
    "已过期": "已废弃",
}
# 重复点击同一个动作时沿用当前已落地的状态；其它跨阶段动作一律挡下。
IDEMPOTENT_STATUS = {"开始导出": "导出中", "标记完成": "已完成", "清理文件": "已过期"}
# 集市状态是导出任务状态的唯一来源，读取时总是覆盖。
MART_SYNC_FIELDS = ["字段选择", "刷新策略", "分区字段"]


class ExportService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for row in rows:
            self._sync_from_mart(row)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int | str) -> dict[str, Any] | None:
        entry = self._resolve(entry_id)
        if entry is not None:
            self._sync_from_mart(entry)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ["字段选择", "刷新策略", "分区字段", "筛选条件", "导出上限", "文件大小"]:
            entry[field] = str(values.get(field) or "")
        entry["status"] = STATUS_ORDER[0]
        entry["任务状态"] = entry["status"]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int | str, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self._resolve(entry_id)
        if entry is None:
            return None, f"导出任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分析导出中心可执行范围"
        code = str(entry.get("任务编号") or entry_id)
        current = str(entry.get("status") or STATUS_ORDER[0])

        mart = self._find_mart(entry)
        if mart is not None:
            # 导出任务状态跟随集市：集市没走到对应阶段时，导出中心不能自己先走。
            self._sync_from_mart(entry)
            current = str(entry.get("status") or STATUS_ORDER[0])
            if IDEMPOTENT_STATUS.get(action) == current:
                return entry, f"导出任务 {code} 已处于「{current}」，沿用当前结果"
            if current == "已过期":
                return None, f"导出任务 {code} 已跟随集市废弃而过期，不能恢复"
            target = MART_STATUS_BY_EXPORT.get(str(mart.get("status") or ""))
            if target == current:
                return None, (
                    f"导出任务 {code} 已跟随集市状态为「{current}」，"
                    f"动作「{action}」不适用；请在数据集市构建中推进集市状态"
                )
            return None, (
                f"导出任务 {code} 当前为「{current}」，集市 {mart.get('集市编号')} 为"
                f"「{mart.get('status')}」；请在数据集市构建中推进集市状态"
                f"（集市状态推进后导出任务会同步到「{target}」）"
            )

        if current == "已过期":
            return None, f"导出任务 {code} 已过期，不能恢复"

        from_status, target = ACTION_RULES[action]
        if current != from_status:
            return None, (
                f"导出任务 {code} 当前为「{current}」，不能执行「{action}」；"
                "状态需按 草稿→导出中→已完成→已过期 连续流转"
            )
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "清理文件" and str(entry.get("文件大小") or "0") in ("0", ""):
            return None, f"导出任务 {code} 没有已落地的文件，清理动作未执行"

        entry["status"] = target
        entry["任务状态"] = target
        entry["pending"] = target not in (STATUS_ORDER[2], STATUS_ORDER[3])
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if target == "已过期":
            entry["文件大小"] = "0"
        return entry, f"导出任务已{action}"

    def _sync_from_mart(self, row: dict[str, Any]) -> None:
        mart = self._find_mart(row)
        if mart is None:
            return
        for field in MART_SYNC_FIELDS:
            row[field] = mart.get(field, row.get(field, ""))
        row["快照版本"] = mart.get("发布快照", row.get("快照版本", ""))
        status = MART_STATUS_BY_EXPORT.get(str(mart.get("status") or ""), row.get("status"))
        row["status"] = status
        row["任务状态"] = status
        row["pending"] = status == "导出中"
        row["abnormal"] = status == "已过期"
        if status == "已过期":
            row["文件大小"] = "0"
        elif status == "已完成" and str(row.get("文件大小") or "0") in ("0", ""):
            row["文件大小"] = "20480"

    @staticmethod
    def _resolve(ref: int | str) -> dict[str, Any] | None:
        """兼容任务编号（EXPO-0001）与内部自增 id 两种定位方式。"""
        text = str(ref).strip()
        if text.isdigit():
            found = store.find(MODULE, int(text))
            if found is not None:
                return found
        return store.find_by_code(MODULE, "任务编号", text)

    @staticmethod
    def _find_mart(row: dict[str, Any]) -> dict[str, Any] | None:
        code = str(row.get("数据源名称") or "").strip()
        if not code:
            return None
        return store.find_by_code(MART_MODULE, MART_CODE_FIELD, code)

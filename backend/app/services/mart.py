"""数据集市构建业务规则：状态流转、字段校验与筛选口径都收在这里。

集市状态只允许按「草稿 → 构建中 → 已发布 → 已废弃」连续流转：
越级动作会被拦下，已发布记录不会倒回构建中，已废弃记录不能恢复。
发布与重建会冻结一份发布快照（导出文件），并以事务方式同步更新分析导出中心；
任何一步校验失败都会把集市定义、导出任务和快照一起回退。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "mart"
EXPORT_MODULE = "export"
EXPORT_CODE_FIELD = "数据源名称"
REQUIRED_FIELDS = ["集市编号", "集市名称", "数据源表"]
OPTIONAL_FIELDS = ["字段选择", "聚合粒度", "刷新策略", "分区字段"]
EDITABLE_FIELDS = ["集市名称", "数据源表", "字段选择", "聚合粒度", "刷新策略", "分区字段"]
DEFINITION_FIELDS = ["集市名称", "数据源表", "字段选择", "聚合粒度", "刷新策略", "分区字段"]

STATUS_ORDER = ["草稿", "构建中", "已发布", "已废弃"]
# 动作 -> (允许的前置状态, 目标状态)。重建集市保持在已发布，只刷新发布结果。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "构建集市": ("草稿", "构建中"),
    "发布集市": ("构建中", "已发布"),
    "废弃集市": ("已发布", "已废弃"),
    "重建集市": ("已发布", "已发布"),
}
NEGATIVE_ACTIONS = ["废弃集市"]
TERMINAL_STATUS = STATUS_ORDER[-1]

# 集市状态变化时，分析导出中心的对应任务同步落到同一阶段。
EXPORT_STATUS_BY_MART = {
    "草稿": "草稿",
    "构建中": "导出中",
    "已发布": "已完成",
    "已废弃": "已过期",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MartService:
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
            rows = [row for row in rows if keyword in str(row.get("集市编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        for row in rows:
            self._sync_display(row)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int | str) -> dict[str, Any] | None:
        entry = self._resolve(entry_id)
        if entry is not None:
            self._sync_display(entry)
        return entry

    def list_snapshots(self) -> tuple[list[dict[str, Any]], int]:
        snapshots = store.snapshots()
        return snapshots, len(snapshots)

    @staticmethod
    def _resolve(ref: int | str) -> dict[str, Any] | None:
        """兼容既有集市编号（MART-0001）与内部自增 id 两种定位方式。"""
        text = str(ref).strip()
        if text.isdigit():
            found = store.find(MODULE, int(text))
            if found is not None:
                return found
        return store.find_by_code(MODULE, "集市编号", text)

    def get_snapshot(self, version: str) -> dict[str, Any] | None:
        return store.find_snapshot(version)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            entry[field] = str(values.get(field) or "").strip()
        entry["status"] = STATUS_ORDER[0]
        entry["集市状态"] = entry["status"]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["发布快照"] = ""
        entry["发布结果"] = None
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int | str,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = dict(values or {})
        entry = self._resolve(entry_id)
        if entry is None:
            return None, f"集市定义 {entry_id} 不存在或已归档"
        action = str(action or "").strip()
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据集市构建可执行范围"
        code = str(entry.get("集市编号") or entry_id)
        current = str(entry.get("status") or STATUS_ORDER[0])

        # 已废弃是终态：任何动作都不能把记录恢复回来。
        if current == TERMINAL_STATUS:
            return None, f"集市 {code} 已废弃，不能恢复，也不允许再执行「{action}」"

        edits = {
            field: str(values[field]).strip()
            for field in EDITABLE_FIELDS
            if field in values and values[field] is not None
        }
        from_status, target = ACTION_RULES[action]

        # 同一发布动作被重复提交：沿用第一次落地的发布结果，不再生成第二份。
        # 需要按新定义重新生成发布结果时走「重建集市」。
        if action == "发布集市" and current == STATUS_ORDER[2]:
            snapshot = entry.get("发布结果") or {}
            version = entry.get("发布快照") or snapshot.get("快照版本") or "—"
            return entry, (
                f"集市 {code} 已发布，沿用第一次发布结果（快照 {version}），"
                "未重复生成发布结果；如需按新定义重出结果请执行「重建集市」"
            )

        if current != from_status:
            return None, (
                f"集市 {code} 当前为「{current}」，不能执行「{action}」；"
                "状态需按 草稿→构建中→已发布→已废弃 连续流转"
            )

        # 目标状态同样必须是序列里的合法值，越级动作在这里被挡下。
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if STATUS_ORDER.index(target) - STATUS_ORDER.index(current) > 1:
            return None, f"集市 {code} 不能从「{current}」越级流转到「{target}」"

        return self._apply_transition(entry, action, target, edits)

    def _apply_transition(
        self,
        entry: dict[str, Any],
        action: str,
        target: str,
        edits: dict[str, str],
    ) -> tuple[dict[str, Any] | None, str]:
        code = str(entry.get("集市编号") or entry.get("id"))
        before_entry = deepcopy(entry)
        before_snapshots = len(store.snapshots())

        export_entry = self._find_export_entry(code)
        created_export = False
        before_export: dict[str, Any] | None = None
        if export_entry is None:
            export_entry = self._create_export_entry(entry)
            created_export = True
        else:
            before_export = deepcopy(export_entry)

        try:
            candidate = deepcopy(entry)
            candidate.update(edits)

            # 只有真正产出发布结果的动作才校验发布字段；废弃只需要记录存在。
            if target == STATUS_ORDER[2]:
                self._validate_publishable(candidate)

            snapshot_version = entry.get("发布快照") or ""
            snapshot_result = entry.get("发布结果") or None
            if target == STATUS_ORDER[2]:
                snapshot_version, snapshot_result = self._freeze_snapshot(candidate, action)

            entry.update(edits)
            entry["status"] = target
            entry["集市状态"] = target
            entry["pending"] = target not in (STATUS_ORDER[2], STATUS_ORDER[3])
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            if target == STATUS_ORDER[2]:
                entry["发布快照"] = snapshot_version
                entry["发布结果"] = snapshot_result
            if target == STATUS_ORDER[3]:
                entry["发布结果"] = {
                    **(snapshot_result or {}),
                    "状态": "已废弃",
                }
                entry["发布快照"] = snapshot_version

            self._sync_export_entry(export_entry, entry, target, snapshot_version)
            return entry, f"集市定义已{action}"
        except ValueError as error:
            self._rollback(entry, before_entry, export_entry, created_export, before_export, before_snapshots)
            return None, f"集市 {code} {action}失败：{error}；集市构建清单与导出文件已一并回退"
        except Exception as error:  # pragma: no cover - 防御性兜底
            self._rollback(entry, before_entry, export_entry, created_export, before_export, before_snapshots)
            return None, f"集市 {code} {action}失败：{error}；集市构建清单与导出文件已一并回退"

    def _validate_publishable(self, candidate: dict[str, Any]) -> None:
        missing = [
            field for field in ["字段选择", "刷新策略", "分区字段"]
            if not str(candidate.get(field) or "").strip()
        ]
        if missing:
            raise ValueError(f"{'、'.join(missing)}不能为空，无法生成发布结果")
        refresh_policy = str(candidate.get("刷新策略") or "").strip()
        if not any(unit in refresh_policy for unit in ("小时", "天", "周", "月", "分钟", "秒")):
            raise ValueError(f"刷新策略「{refresh_policy}」无法识别，发布未执行")

    def _freeze_snapshot(
        self,
        candidate: dict[str, Any],
        action: str,
    ) -> tuple[str, dict[str, Any]]:
        code = str(candidate.get("集市编号") or candidate.get("id"))
        current_version = str(candidate.get("发布快照") or "")
        current_item = self._snapshot_item(current_version, code) if current_version else None

        # 定义与上一次发布结果完全一致时复用快照：重复点击只落一份发布结果。
        if current_item is not None and self._definition_matches(current_item, candidate):
            snapshot = store.find_snapshot(current_version)
            generated_at = str((snapshot or {}).get("生成时间") or _now())
            return current_version, {
                "快照版本": current_version,
                "生成时间": generated_at,
                "触发动作": str((snapshot or {}).get("触发动作") or action),
                "导出任务编号": str((snapshot or {}).get("导出任务编号") or ""),
                "字段选择": candidate.get("字段选择"),
                "刷新策略": candidate.get("刷新策略"),
                "分区字段": candidate.get("分区字段"),
                "状态": "已发布",
            }

        version = store.next_snapshot_version()
        item = {
            "集市编号": code,
            "集市名称": candidate.get("集市名称"),
            "数据源表": candidate.get("数据源表"),
            "字段选择": candidate.get("字段选择"),
            "聚合粒度": candidate.get("聚合粒度"),
            "刷新策略": candidate.get("刷新策略"),
            "分区字段": candidate.get("分区字段"),
            "集市状态": "已发布",
        }
        snapshot = {
            "快照版本": version,
            "生成时间": _now(),
            "触发动作": action,
            "导出任务编号": "",
            "条目": [item],
        }
        store.append_snapshot(snapshot)
        return version, {
            "快照版本": version,
            "生成时间": snapshot["生成时间"],
            "触发动作": action,
            "导出任务编号": "",
            "字段选择": item["字段选择"],
            "刷新策略": item["刷新策略"],
            "分区字段": item["分区字段"],
            "状态": "已发布",
        }

    def _sync_export_entry(
        self,
        export_entry: dict[str, Any],
        entry: dict[str, Any],
        target: str,
        snapshot_version: str,
    ) -> None:
        export_entry["数据源名称"] = str(entry.get("集市编号") or entry.get("id"))
        export_entry["字段选择"] = entry.get("字段选择", "")
        export_entry["刷新策略"] = entry.get("刷新策略", "")
        export_entry["分区字段"] = entry.get("分区字段", "")
        export_entry["任务状态"] = EXPORT_STATUS_BY_MART.get(target, "草稿")
        export_entry["status"] = EXPORT_STATUS_BY_MART.get(target, "草稿")
        export_entry["pending"] = target == "构建中"
        export_entry["快照版本"] = snapshot_version or export_entry.get("快照版本", "")
        if target == "已废弃":
            export_entry["文件大小"] = "0"
        elif target == "已发布":
            if str(export_entry.get("文件大小") or "0") in ("0", ""):
                export_entry["文件大小"] = "20480"

        result = entry.get("发布结果")
        if snapshot_version and result and not result.get("导出任务编号"):
            result["导出任务编号"] = str(export_entry.get("任务编号") or "")
            snapshot = store.find_snapshot(snapshot_version)
            if snapshot is not None and not snapshot.get("导出任务编号"):
                snapshot["导出任务编号"] = str(export_entry.get("任务编号") or "")

    def _find_export_entry(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(EXPORT_MODULE):
            if str(row.get(EXPORT_CODE_FIELD, "").strip()) == code:
                return row
        return None

    def _create_export_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(EXPORT_MODULE)
        new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        used_codes = {str(row.get("任务编号", "")) for row in rows}
        task_code = f"EXPO-{new_id:04d}"
        suffix = new_id
        while task_code in used_codes:
            suffix += 1
            task_code = f"EXPO-{suffix:04d}"
        row = {
            "id": new_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "任务编号": task_code,
            "数据源名称": str(entry.get("集市编号") or entry.get("id")),
            "导出格式": "CSV",
            "字段选择": entry.get("字段选择", ""),
            "刷新策略": entry.get("刷新策略", ""),
            "分区字段": entry.get("分区字段", ""),
            "筛选条件": "无",
            "导出上限": "100000",
            "文件大小": "0",
            "任务状态": "草稿",
            "快照版本": "",
        }
        rows.append(row)
        return row

    @staticmethod
    def _snapshot_item(version: str, code: str) -> dict[str, Any] | None:
        snapshot = store.find_snapshot(version)
        if snapshot is None:
            return None
        for item in snapshot.get("条目", []):
            if str(item.get("集市编号", "")) == code:
                return item
        return None

    @staticmethod
    def _definition_matches(snapshot_item: dict[str, Any], candidate: dict[str, Any]) -> bool:
        for field in DEFINITION_FIELDS:
            if str(snapshot_item.get(field) or "") != str(candidate.get(field) or ""):
                return False
        return True

    @staticmethod
    def _rollback(
        entry: dict[str, Any],
        before_entry: dict[str, Any],
        export_entry: dict[str, Any],
        created_export: bool,
        before_export: dict[str, Any] | None,
        before_snapshots: int,
    ) -> None:
        entry.clear()
        entry.update(before_entry)
        if created_export:
            rows = store.rows(EXPORT_MODULE)
            if export_entry in rows:
                rows.remove(export_entry)
        elif before_export is not None:
            export_entry.clear()
            export_entry.update(before_export)
        store.truncate_snapshots(before_snapshots)

    @staticmethod
    def _sync_display(row: dict[str, Any]) -> None:
        """列表里的「集市状态」始终跟真实状态字段一致，避免回执与列表各说各话。"""
        row["集市状态"] = row.get("status", STATUS_ORDER[0])

"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
集市发布快照（导出文件）与业务模块分开存放：快照是不可变的发布结果，
不会出现在运营概览的业务模块统计里。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.seed import SEED_ROWS, SEED_SNAPSHOTS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: deepcopy(rows) for name, rows in SEED_ROWS.items()
        }
        # 发布快照是只追加的版本序列，不参与 overview 的业务模块统计。
        self._snapshots: list[dict[str, Any]] = deepcopy(SEED_SNAPSHOTS)

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_code(self, module: str, field: str, code: str) -> dict[str, Any] | None:
        """按业务编号（如集市编号）读取记录，兼容既有编号与后续新增编号。"""
        for row in self.rows(module):
            if str(row.get(field, "")).strip() == str(code).strip():
                return row
        return None

    def snapshots(self) -> list[dict[str, Any]]:
        return self._snapshots

    def find_snapshot(self, version: str) -> dict[str, Any] | None:
        for snapshot in self._snapshots:
            if str(snapshot.get("快照版本", "")).upper() == str(version).strip().upper():
                return snapshot
        return None

    def next_snapshot_version(self) -> str:
        """版本号沿用 ``V<数字>``；既有 V7 之后从 V8 开始，避免覆盖历史快照。"""
        highest = 0
        for snapshot in self._snapshots:
            raw = str(snapshot.get("快照版本", ""))
            if raw.upper().startswith("V") and raw[1:].isdigit():
                highest = max(highest, int(raw[1:]))
        return f"V{highest + 1}"

    def append_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self._snapshots.append(snapshot)
        return snapshot

    def truncate_snapshots(self, length: int) -> None:
        """回退发布时把本次追加的快照一起撤掉，保证清单与导出文件同进同退。"""
        del self._snapshots[length:]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()

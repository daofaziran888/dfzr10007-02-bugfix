"""集市发布调用链回归：集市动作 → 集市定义读取 → 分析导出入口。

不依赖 pytest/httpx：先按 ``app.main:app`` 启动服务，再对真实接口发请求。
用法：``python3 backend/tests/regression_mart_export.py``（需先起后端）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("MART_TEST_BASE", "http://127.0.0.1:8000")

FAILURES: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8") or "{}")


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name} {detail}")


def action(mart_id: int | str, act: str, **values) -> dict:
    status, payload = call(
        "POST", f"/api/mart/{mart_id}/actions", {"values": {"action": act, **values}}
    )
    assert status == 200, (status, payload)
    return payload


def mart(id_or_code: int | str) -> dict:
    status, payload = call("GET", f"/api/mart/{id_or_code}")
    assert status == 200, (status, payload)
    return payload


def export_list() -> dict[str, dict]:
    status, payload = call("GET", "/api/export")
    assert status == 200, (status, payload)
    return {item["数据源名称"]: item for item in payload["items"]}


def snapshots() -> list[dict]:
    status, payload = call("GET", "/api/mart/snapshots")
    assert status == 200, (status, payload)
    return payload["items"]


def main() -> int:
    print("== 初始口径 ==")
    rows = export_list()
    check("V7 保留 MART-0001 每小时刷新策略", rows["MART-0001"]["刷新策略"] == "每小时")
    check("V7 保留 MART-0003 旧分区字段 dt", rows["MART-0003"]["分区字段"] == "dt")
    v7 = call("GET", "/api/mart/snapshots/V7")[1]
    items = {item["集市编号"]: item for item in v7["条目"]}
    check("导出文件 V7 保留 MART-0001 原值", items["MART-0001"]["刷新策略"] == "每小时")
    check("导出文件 V7 保留 MART-0003 原值", items["MART-0003"]["分区字段"] == "dt")
    check("列表页与导出入口同一份字段选择",
          items["MART-0001"]["字段选择"] == rows["MART-0001"]["字段选择"])

    print("== MART-0001 草稿：发布集市失败 ==")
    before = len(snapshots())
    payload = action(1, "发布集市")
    check("草稿直接发布被挡下", payload["ok"] is False, payload["message"])
    check("失败不落发布结果", len(snapshots()) == before)
    check("集市仍在草稿", mart(1)["status"] == "草稿")
    check("导出中心仍是草稿", export_list()["MART-0001"]["任务状态"] == "草稿")

    print("== MART-0001：构建 → 发布 → 重复发布 ==")
    payload = action(1, "构建集市")
    check("草稿可进入构建中", payload["ok"] is True and mart(1)["status"] == "构建中")
    payload = action(1, "发布集市")
    check("构建中可发布", payload["ok"] is True and mart(1)["status"] == "已发布")
    first_version = payload["entry"]["发布快照"]
    first = len(snapshots())
    repeat = action(1, "发布集市")
    check("重复发布沿用第一次结果", repeat["ok"] is True and repeat["entry"]["发布快照"] == first_version)
    check("重复发布只落一份结果", len(snapshots()) == first)
    row = export_list()["MART-0001"]
    check("导出中心同步为已完成", row["任务状态"] == "已完成", row["任务状态"])
    check("导出中心字段选择与集市一致", row["字段选择"] == mart(1)["字段选择"])
    check("导出中心刷新策略与集市一致", row["刷新策略"] == "每小时")

    print("== MART-0003 已发布：重建集市 ==")
    code_before = mart(3)["发布快照"]
    payload = action(3, "重建集市", 字段选择="store_id,visitor_cnt")
    check("重建不再写成已废弃", payload["ok"] is True and mart(3)["status"] == "已发布", mart(3)["status"])
    new_version = payload["entry"]["发布快照"]
    check("重建生成新的发布结果", new_version != code_before, f"{code_before} -> {new_version}")
    rebuilt = len(snapshots())
    action(3, "重建集市")
    check("重复重建沿用第一次结果", len(snapshots()) == rebuilt and mart(3)["发布快照"] == new_version)
    check("导出中心展示新字段选择",
          export_list()["MART-0003"]["字段选择"] == "store_id,visitor_cnt")
    check("导出入口与列表页同一份字段选择",
          export_list()["MART-0003"]["字段选择"] == mart(3)["字段选择"])

    print("== 越级与恢复 ==")
    payload = action(3, "构建集市")
    check("已发布不能倒回构建中", payload["ok"] is False)
    payload = action(3, "废弃集市")
    check("已发布可废弃", payload["ok"] is True and mart(3)["status"] == "已废弃")
    check("废弃后导出中心为已过期", export_list()["MART-0003"]["任务状态"] == "已过期")
    payload = action(3, "发布集市")
    check("已废弃不能恢复", payload["ok"] is False)
    check("已废弃仍为已废弃", mart(3)["status"] == "已废弃")
    payload = action(3, "重建集市")
    check("已废弃不能重建", payload["ok"] is False)

    print("== 发布失败整体回退 ==")
    action(2, "构建集市") if mart(2)["status"] == "草稿" else None
    before_snapshots = len(snapshots())
    before_export = export_list()["MART-0002"].copy()
    payload = action(2, "发布集市", 字段选择="   ")
    check("空字段选择导致发布失败", payload["ok"] is False, payload["message"])
    check("失败后集市仍在构建中", mart(2)["status"] == "构建中")
    check("失败后字段选择未被改写", mart(2)["字段选择"] != "")
    check("失败后快照未增加", len(snapshots()) == before_snapshots)
    after_export = export_list()["MART-0002"]
    check("失败后导出文件回退",
          after_export["任务状态"] == before_export["任务状态"]
          and after_export["字段选择"] == before_export["字段选择"])

    print("== 分析导出中心跟随状态变化 ==")
    # MART-0002 仍在构建中，导出任务只能是「导出中」，不能自己先标完成。
    before_export_row = export_list()["MART-0002"].copy()
    check("集市构建中时导出任务为导出中", before_export_row["任务状态"] == "导出中")
    direct = call("POST", "/api/export/EXPO-0002/actions",
                  {"values": {"action": "标记完成"}})
    check("导出中心不能自己越过集市状态", direct[1]["ok"] is False, direct[1]["message"])
    check("导出中心任务状态未被改写",
          export_list()["MART-0002"]["任务状态"] == before_export_row["任务状态"])
    repeat = call("POST", "/api/export/EXPO-0001/actions",
                  {"values": {"action": "标记完成"}})
    check("重复动作沿用第一次结果", repeat[1]["ok"] is True
          and export_list()["MART-0001"]["任务状态"] == "已完成")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：{', '.join(FAILURES)}")
        return 1
    print("全部回归通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

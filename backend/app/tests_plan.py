"""表优化执行计划的服务级自测：不依赖 HTTP，直接跑一遍完整链路。

用法：
    cd backend && .venv/bin/python -m app.tests_plan
"""
from __future__ import annotations

from app.services.iceberg import IcebergService
from app.services.plan import IcebergPlanService
from app.store import store


def main() -> None:
    service = IcebergPlanService()
    config = service.get_config()
    assert config["资源总预算"] == "800 GB"
    assert config["行组大小上限"] == "512 MB"

    # 1. 候选视图：读任务 + 容量统计，按收益比例排序，ods_orders 有真实的优化前后口径。
    candidates = service.candidates()
    ods_orders = next(item for item in candidates["items"] if item["表名称"] == "ods_orders")
    assert ods_orders["优化前容量"] == "1.2 TB"
    assert ods_orders["优化前文件数"] == 25400
    assert ods_orders["优化后文件数"] < ods_orders["优化前文件数"]
    by_gain = [item for item in service.candidates(sort_by="收益比例")["items"] if item["可编排"]]
    assert [item["收益比例值"] for item in by_gain] == sorted(
        (item["收益比例值"] for item in by_gain), reverse=True
    )
    by_saving = [item for item in service.candidates(sort_by="预计收益")["items"] if item["可编排"]]
    assert [item["预计收益字节"] for item in by_saving] == sorted(
        (item["预计收益字节"] for item in by_saving), reverse=True
    )

    # 2. 同表只保留一个活动任务。
    duplicates = candidates["同表重复任务"]
    assert duplicates and duplicates[0]["表名称"] == "dwd_payment"
    task, missing = IcebergService().create_entry(
        {"任务编号": "ICEB-X", "表名称": "ods_orders", "优化类型": "小文件合并"}
    )
    assert task is None and "同表只允许一个活动优化任务" in missing[0]

    # 3. 编排：不超预算，预算不够时跳过当前候选、继续比对剩余候选。
    plan, message = service.create_plan({"排序": "收益比例"})
    assert plan and plan["状态"] == "已保存" and plan["版本"] == 1
    assert plan["已用预算字节"] <= config["资源总预算字节"]
    assert plan["入选数量"] < plan["候选总数"]
    assert len({entry["表名称"] for entry in plan["entries"]}) == len(plan["entries"])
    assert any("预算不足" in item["原因"] for item in plan["skipped"])

    # 4. 已保存计划会预留预算，新计划不能把同一张表重复算收益。
    second, second_message = service.create_plan({"排序": "收益比例"})
    assert second is None and "没有可以编排的表" in second_message
    assert len(store.rows("_iceberg_reconcile")) == len(plan["entries"])

    # 5. 版本冲突：可选保留原任务或改排新任务；改排后版本 +1，核对清单同版本。
    conflicts = service.detect_conflicts(plan["id"])
    assert conflicts and plan["状态"] == "版本冲突"
    blocked, blocked_message = service.run_action(plan["id"], "执行计划")
    assert blocked is None and "版本冲突" in blocked_message
    resolved, _ = service.resolve_conflict(conflicts[0]["id"], "改排新任务")
    assert resolved and resolved["版本"] == 2
    assert {row["计划版本"] for row in service.list_reconcile(plan_id=plan["id"])} == {2}

    # 6. 执行：只回写被选任务的实际效果，统计信息与核对清单都落在当前版本。
    executed, _ = service.run_action(plan["id"], "执行计划")
    assert executed and executed["状态"] == "已完成"
    reports = [row for row in store.rows("stats") if row.get("计划编号") == plan["计划编号"]]
    assert len(reports) == len(executed["entries"])
    assert all(row["计划版本"] == executed["版本"] for row in reports)
    assert all(row["优化前容量"] and row["优化后容量"] for row in reports)
    reconcile = service.list_reconcile(plan_id=plan["id"])
    assert all(row["核对状态"] in {"已核对", "有差异"} for row in reconcile)
    assert all(row["实际收益"] != "—" for row in reconcile)

    # 7. 恢复结论记入预算核对清单。
    restored, _ = service.run_action(plan["id"], "恢复", "恢复结论：容量与文件数已核对")
    assert restored and restored["恢复结论"].startswith("恢复结论")
    reconcile = service.list_reconcile(plan_id=plan["id"])
    assert all(row["核对状态"] == "已恢复" and row["恢复结论"] for row in reconcile)
    assert service.get_plan(plan["id"])["版本核对"]["一致"] is True

    print("表优化执行计划自测通过")


if __name__ == "__main__":
    main()

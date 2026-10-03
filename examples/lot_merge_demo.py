#!/usr/bin/env python3
"""食品换线放行服务 —— 原料批号并档端到端调用示例（仅用标准库）。

演示流程（独立产线 LMERGE-1 / LMERGE-2，批号 MG-RM-*、批次 MB*，避免与其他示例冲突）：
  1. 准备：原料批号 MG-RM-S（peanut，误登记的重复编号）、MG-RM-T（peanut，保留编号）、
     MG-RM-L（milk，已待检锁定的目标批号）；
     产线 LMERGE-1：MB1 用 MG-RM-S 声明 peanut（RELEASE，残留 peanut）
                    MB2 同时引用 MG-RM-S 与 MG-RM-T、声明 peanut（并档后去重）
                    MB3 无原料未清洁（继承残留）
     产线 LMERGE-2：MC1 用 MG-RM-S 声明 peanut。
  2. 创建并确认核验单 MG-<时间戳>-1：[MB1, MB3]。
  3. 拒绝场景：相同编号 409、未知目标批号 404、空原因 400（失败不留部分引用或版本变更）。
  4. 并档 MG-RM-S -> MG-RM-T：
       有效批次 MB1/MB2/MC1 直接用料改指 MG-RM-T（MB2 去重）；
       两条产线各自从最早引用批次起重算并追加 MERGE_LOT 裁决版本；
       已确认核验单转待复核，历次确认快照不改；
       源批号留档可查（status=MERGED），目标批号名称/过敏原/锁定状态不变。
  5. 源批号停用：新批次引用 400、更正批号 409、锁定 409、再次并档 409；详情仍可查询。
  6. 出货资格按目标号锁定状态更新：MB4 用 MG-RM-K（待检锁定源号），
     并档到未锁定的 MG-RM-T 后出货资格 BLOCKED -> SHIPPABLE（版本递增，MERGE_LOT）。
  7. 已发运批次仍按原交接追溯：MB5 确认交接后并档，交接记录与逐批冻结快照不改写。

用法：
    python examples/lot_merge_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/lot_merge_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE1 = "LMERGE-1"
LINE2 = "LMERGE-2"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, payload: dict) -> dict:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        print(f"  已登记批次 {batch_no}（{body['line_id']} seq={body['seq']}）"
              f" => {adj['disposition']}（v{adj['version']}，实际={adj['actual_allergens']}）")
        return body
    if status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
        return call("GET", f"/api/batches/{batch_no}")[1]
    raise RuntimeError(f"登记批次失败: {body}")


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))

    section("1. 准备：误登记的重复编号 MG-RM-S 与保留编号 MG-RM-T；两线批次")
    ensure_lot("MG-RM-S", "花生酱（误登记重复编号）", ["peanut"])
    ensure_lot("MG-RM-T", "花生酱", ["peanut"])
    ensure_lot("MG-RM-K", "奶粉（待检锁定源号）", ["milk"])
    ensure_batch("MB1", {
        "batch_no": "MB1", "line_id": LINE1,
        "material_lots": ["MG-RM-S"],
        "declared_allergens": ["peanut"], "cleaned_allergens": [],
    })
    ensure_batch("MB2", {
        "batch_no": "MB2", "line_id": LINE1,
        "material_lots": ["MG-RM-S", "MG-RM-T"],   # 同时引用两号，并档后去重
        "declared_allergens": ["peanut"], "cleaned_allergens": ["peanut"],
    })
    mb3 = ensure_batch("MB3", {
        "batch_no": "MB3", "line_id": LINE1,
        "material_lots": [],
        "declared_allergens": ["peanut"], "cleaned_allergens": [],
    })
    assert mb3["adjudication"]["disposition"] == "RELEASE", mb3  # 继承残留已声明
    ensure_batch("MC1", {
        "batch_no": "MC1", "line_id": LINE2,
        "material_lots": ["MG-RM-S"],
        "declared_allergens": ["peanut"], "cleaned_allergens": [],
    })

    doc_no = f"MG-{suffix}-1"
    section(f"2. 创建并确认核验单 {doc_no}：[MB1, MB3]")
    status, body = call("POST", "/api/verifications", {
        "verification_no": doc_no, "batch_nos": ["MB1", "MB3"],
    })
    assert status == 201, body
    status, doc = call("POST", f"/api/verifications/{doc_no}/confirm")
    assert status == 200 and doc["status"] == "CONFIRMED", doc
    print(f"  核验单 {doc_no} 已确认（revision={doc['revision']}）")

    section("3. 拒绝场景：相同编号 / 未知目标批号 / 空原因（整体拒绝，不留部分变更）")
    status, body = call("POST", "/api/material-lots/MG-RM-S/merge",
                        {"target_lot_no": "MG-RM-S", "reason": "相同编号"})
    print(f"[{status}] 相同编号: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "SAME_SOURCE_TARGET_LOT", body

    status, body = call("POST", "/api/material-lots/MG-RM-S/merge",
                        {"target_lot_no": "NO-SUCH-LOT", "reason": "未知目标"})
    print(f"[{status}] 未知目标批号: {body['error']['code']}")
    assert status == 404 and body["error"]["code"] == "MATERIAL_LOT_NOT_FOUND", body

    status, body = call("POST", "/api/material-lots/MG-RM-S/merge",
                        {"target_lot_no": "MG-RM-T", "reason": "   "})
    print(f"[{status}] 空原因: {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "INVALID_PAYLOAD", body
    # 失败不留部分引用或版本变更
    still = call("GET", "/api/batches/MB1")[1]
    assert still["material_lots"] == ["MG-RM-S"] and still["adjudication"]["version"] == 1
    assert call("GET", "/api/material-lot-merges")[1]["merges"] == []
    print("  校验：MB1 仍引用 MG-RM-S、裁决版本仍为 v1、无并档记录 —— 整体回滚符合预期")

    section("4. 并档：MG-RM-S -> MG-RM-T（有效批次改指、两线重算、核验单转待复核）")
    status, merged = call("POST", "/api/material-lots/MG-RM-S/merge", {
        "target_lot_no": "MG-RM-T",
        "reason": "同一原料（花生酱）误登记为两个编号，并档保留 MG-RM-T",
    })
    show("并档结果（源/目标批号、改指批次、重算、受影响核验单）", status, merged)
    assert status == 201, merged

    # 源批号留档停用；目标批号保留自身名称/过敏原/锁定状态
    assert merged["source_lot"]["status"] == "MERGED"
    assert merged["source_lot"]["merged_into"] == "MG-RM-T"
    assert merged["target_lot"]["status"] == "ACTIVE"
    assert merged["target_lot"]["material"] == "花生酱"

    # 有效批次改指：MB2 原本同时引用两号 -> 去重
    redirected = {r["batch_no"]: r for r in merged["redirected_batches"]}
    assert set(redirected) == {"MB1", "MB2", "MC1"}, redirected
    assert redirected["MB2"]["to_material_lots"] == ["MG-RM-T"]
    assert redirected["MB2"]["deduplicated"] is True
    print("  改指：MB1/MB2/MC1 用料已指向 MG-RM-T（MB2 去重）")

    # 两条产线各自从最早引用批次起重算（trigger=MERGE_LOT）
    recomputed = {s["batch_no"]: s for s in merged["recomputed"]}
    assert set(recomputed) == {"MB1", "MB2", "MB3", "MC1"}, recomputed
    hist = call("GET", "/api/batches/MB3/history")[1]["history"]
    print(f"  MB3 历史版本：{[(v['version'], v['trigger']) for v in hist]}")
    assert hist[-1]["trigger"] == "MERGE_LOT"

    # 已确认核验单转待复核；历次确认快照不改
    affected = {v["verification_no"]: v for v in merged["affected_verifications"]}
    assert affected.get(doc_no, {}).get("status") == "PENDING_REVIEW", affected
    doc = call("GET", f"/api/verifications/{doc_no}")[1]
    assert doc["status"] == "PENDING_REVIEW"
    assert len(doc["confirmations"]) == 1
    assert doc["confirmations"][0]["batches"][0]["version"] == 1
    print(f"  核验单 {doc_no} 已转待复核；首次确认快照仍冻结 v1")

    section("5. 源批号留档可查，但不再接受新引用 / 更正 / 锁定 / 再次并档")
    status, lot = call("GET", "/api/material-lots/MG-RM-S")
    print(f"[{status}] 源批号留档：status={lot['status']} merged_into={lot['merged_into']}")
    assert lot["status"] == "MERGED" and lot["merged_into"] == "MG-RM-T"

    status, body = call("POST", "/api/batches", {
        "batch_no": f"MBX-{suffix}", "line_id": LINE1,
        "material_lots": ["MG-RM-S"], "declared_allergens": [], "cleaned_allergens": [],
    })
    print(f"[{status}] 新批次引用已停用批号: {body['error']['code']}")
    assert status == 400 and body["error"]["code"] == "MATERIAL_LOT_MERGED", body

    status, body = call("PUT", "/api/material-lots/MG-RM-S",
                        {"allergens": ["peanut", "milk"]})
    print(f"[{status}] 更正已停用批号: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "MATERIAL_LOT_MERGED", body

    status, body = call("POST", "/api/material-lots/MG-RM-S/lock",
                        {"reason": "尝试锁定已停用批号"})
    print(f"[{status}] 锁定已停用批号: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "MATERIAL_LOT_MERGED", body

    status, body = call("POST", "/api/material-lots/MG-RM-S/merge",
                        {"target_lot_no": "MG-RM-K", "reason": "再次并档"})
    print(f"[{status}] 再次并档已停用批号: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "MATERIAL_LOT_MERGED", body

    merges = call("GET", "/api/material-lot-merges")[1]["merges"]
    print(f"  并档记录（只追加）：{[(m['source_lot_no'], m['target_lot_no']) for m in merges]}")
    assert any(m["source_lot_no"] == "MG-RM-S" and m["target_lot_no"] == "MG-RM-T" for m in merges)

    section("6. 出货资格按目标号当前锁定状态更新（锁定源号并入未锁定目标）")
    ensure_batch("MB4", {
        "batch_no": "MB4", "line_id": LINE2,
        "material_lots": ["MG-RM-K"],
        "declared_allergens": ["milk"], "cleaned_allergens": ["peanut"],
    })
    status, _ = call("POST", "/api/material-lots/MG-RM-K/lock",
                     {"reason": "供应商通报待检风险"})
    assert status == 200
    elig = call("GET", "/api/batches/MB4/eligibility")[1]
    print(f"  锁定源号后 MB4 出货资格：v{elig['eligibility']['version']} "
          f"{elig['eligibility']['status']}（锁定批号 {elig['eligibility']['locked_lots']}）")
    assert elig["eligibility"]["status"] == "BLOCKED"

    status, merged2 = call("POST", "/api/material-lots/MG-RM-K/merge", {
        "target_lot_no": "MG-RM-T",
        "reason": "同一原料误登记两个编号，并档；目标批号未锁定",
    })
    assert status == 201, merged2
    changes = merged2["shipment_eligibility"]
    print(f"  并档后出货资格变化：{[(c['batch_no'], c['version'], c['status']) for c in changes]}")
    assert [c["batch_no"] for c in changes] == ["MB4"]
    assert changes[0]["status"] == "SHIPPABLE"
    elig = call("GET", "/api/batches/MB4/eligibility")[1]
    assert elig["eligibility"]["status"] == "SHIPPABLE"
    assert elig["history"][-1]["trigger"] == "MERGE_LOT"
    print(f"  MB4 出货资格 v{elig['eligibility']['version']} SHIPPABLE"
          f"（trigger=MERGE_LOT），按目标号当前锁定状态更新")

    section("7. 已发运批次仍按原交接追溯（交接记录与冻结快照不改写）")
    ensure_batch("MB5", {
        "batch_no": "MB5", "line_id": LINE2,
        "material_lots": ["MG-RM-T"],
        "declared_allergens": ["peanut"], "cleaned_allergens": ["milk"],
    })
    ensure_lot("MG-RM-D", "可可块（误登记重复编号）", [])
    ensure_lot("MG-RM-C", "可可块", [])
    # 让 MB5 改用待并档的源号 MG-RM-D
    status, body = call("PUT", "/api/batches/MB5", {"material_lots": ["MG-RM-D"]})
    assert status == 200, body
    doc2 = f"MG-{suffix}-2"
    status, body = call("POST", "/api/verifications", {
        "verification_no": doc2, "batch_nos": ["MB5"],
    })
    assert status == 201, body
    status, doc2_body = call("POST", f"/api/verifications/{doc2}/confirm")
    assert status == 200 and doc2_body["status"] == "CONFIRMED", doc2_body
    ho_no = f"HO-MG-{suffix}"
    status, ho = call("POST", "/api/handovers", {
        "handover_no": ho_no, "verification_no": doc2,
        "expected_revision": doc2_body["revision"],
    })
    assert status == 201, ho
    before = call("GET", f"/api/handovers/{ho_no}")[1]

    status, merged3 = call("POST", "/api/material-lots/MG-RM-D/merge", {
        "target_lot_no": "MG-RM-C",
        "reason": "同一原料误登记两个编号，并档",
    })
    assert status == 201, merged3
    after = call("GET", f"/api/handovers/{ho_no}")[1]
    assert after == before, "交接记录不得因并档改写"
    where = call("GET", "/api/batches/MB5/handover")[1]
    assert where["shipped"] is True and where["handover"]["handover_no"] == ho_no
    print(f"  交接 {ho_no} 记录与逐批冻结快照保持原样；MB5 仍按原交接追溯（shipped=true）")

    section("完成")
    print("MG-RM-S / MG-RM-K / MG-RM-D 已分别并档，源号留档可查且不再接受新引用、更正或锁定；")
    print("有效批次改指目标号并去重、两线按序重算（MERGE_LOT）、出货资格按目标号锁定状态更新、")
    print("已确认核验单转待复核、已发运批次仍按原交接追溯，均符合预期。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

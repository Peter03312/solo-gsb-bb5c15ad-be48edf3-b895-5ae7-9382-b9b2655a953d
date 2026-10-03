#!/usr/bin/env python3
"""食品换线放行服务 —— 已投产批次号更正（标识更正）端到端调用示例（仅用标准库）。

演示流程（独立产线 RN-LINE，批号 RN-B*，避免与其他示例冲突）：
  1. 准备：登记原料批号 RN-RM-M（milk）与批次 RN-B1（声明 milk，RELEASE），
     创建并确认核验单 RN-V-<时间戳>，完成装车交接 RN-H-<时间戳>。
  2. 拒绝场景：未知批次 404、空原因 400、新号与现行号相同 409、
     新号已被现行号占用 409（失败不留部分更名）。
  3. 编号更正 RN-B1 -> RN-B101：只改标识——产线/顺序号、裁决/出货资格版本、
     核验单状态与修订号均不变，发运占用不释放；历次旧号成为查询别名。
  4. 按旧号 RN-B1 仍可查询批次/历史/交接归属，并同时返回历史号与现行号；
     凭旧号不得再登记、不得更正/作废（409），新核验清单只接受现行号（400）。
  5. 历史快照保留记录时号：已确认核验快照、交接冻结快照逐批 recorded_batch_no=RN-B1、
     current_batch_no=RN-B101；可凭旧号登记原交接的退回并完成发运后复核处置。
  6. 连续更正 RN-B101 -> RN-B102：旧号链 [RN-B1, RN-B101] 都保留为别名、都可追溯。
  7. 只追加的更正记录：GET /api/batch-renames（可按现行号/历史号过滤同一批）。

用法：
    python examples/batch_rename_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/batch_rename_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "RN-LINE"


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
        print(f"  已登记批次 {batch_no} => {body['adjudication']['disposition']}")
        return body
    if status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
        return call("GET", f"/api/batches/{batch_no}")[1]
    raise RuntimeError(f"登记批次失败: {body}")


def rename(old: str, new: str, reason: str) -> tuple[int, dict]:
    return call("POST", f"/api/batches/{old}/rename",
                {"new_batch_no": new, "reason": reason})


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")

    suffix = str(int(time.time()))
    vno = f"RN-V-{suffix}"
    hno = f"RN-H-{suffix}"
    rno = f"RN-R-{suffix}"

    section("1. 准备：原料/批次（RELEASE）→ 确认核验单 → 装车交接")
    ensure_lot("RN-RM-M", "更名示例奶粉", ["milk"])
    b1 = ensure_batch("RN-B1", {
        "batch_no": "RN-B1", "line_id": LINE,
        "material_lots": ["RN-RM-M"],
        "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    assert b1["adjudication"]["version"] == 1
    s, _ = call("POST", "/api/verifications",
                {"verification_no": vno, "batch_nos": ["RN-B1"]})
    assert s == 201
    s, doc = call("POST", f"/api/verifications/{vno}/confirm")
    assert s == 200 and doc["status"] == "CONFIRMED"
    revision = doc["revision"]
    s, ho = call("POST", "/api/handovers", {
        "handover_no": hno, "verification_no": vno, "expected_revision": revision,
    })
    assert s == 201, ho
    print(f"  核验单 {vno} 已确认（revision={revision}），交接 {hno} 完成")

    section("2. 拒绝场景：未知批次/空原因/相同号/占用号（失败不留部分更名）")
    s, body = rename("RN-NOPE", "RN-X", "录错")
    print(f"[{s}] 更正未知批次: {body['error']['code']}")
    assert s == 404 and body["error"]["code"] == "BATCH_NOT_FOUND"

    s, body = rename("RN-B1", "RN-X", "   ")
    print(f"[{s}] 空原因: {body['error']['code']}")
    assert s == 400 and body["error"]["code"] == "INVALID_PAYLOAD"

    s, body = rename("RN-B1", "RN-B1", "录错")
    print(f"[{s}] 新号与现行号相同: {body['error']['code']}")
    assert s == 409 and body["error"]["code"] == "SAME_SOURCE_TARGET_BATCH_NO"

    ensure_batch("RN-B9", {
        "batch_no": "RN-B9", "line_id": LINE,
        "material_lots": ["RN-RM-M"],
        "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    s, body = rename("RN-B1", "RN-B9", "录错")
    print(f"[{s}] 新号已被现行号占用: {body['error']['code']}"
          f"（occupied_as={body['error']['details'][0]['occupied_as']}）")
    assert s == 409 and body["error"]["code"] == "BATCH_NO_ALREADY_TAKEN"
    # 失败不留部分更名：RN-B1 仍是现行号
    assert call("GET", "/api/batches/RN-B1")[1]["batch_no"] == "RN-B1"
    assert call("GET", "/api/batch-renames")[1]["renames"] == []

    section("3. 编号更正 RN-B1 -> RN-B101（只改标识）")
    s, body = rename("RN-B1", "RN-B101", "现场发现已投产批次号录错，更正为正确编号")
    show("更正结果", s, body)
    assert s == 200, body
    renamed = body["renamed"]
    assert renamed["batch_no"] == "RN-B101"
    assert renamed["line_id"] == LINE
    assert renamed["adjudication"]["version"] == 1          # 裁决版本不变
    assert renamed["shipment_eligibility"]["version"] == 1  # 资格版本不变
    assert body["identity"]["previous_batch_nos"] == ["RN-B1"]

    # 核验单状态/修订号不变；当前成员以现行号关联
    doc = call("GET", f"/api/verifications/{vno}")[1]
    print(f"核验单 {vno}: status={doc['status']} revision={doc['revision']}（更名前 {revision}）")
    assert doc["status"] == "CONFIRMED" and doc["revision"] == revision
    rec = doc["batches"][0]
    assert rec["batch_no"] == "RN-B101"
    assert rec["previous_batch_nos"] == ["RN-B1"]
    assert rec["changed"] is False and rec["shipment_changed"] is False

    # 发运占用不释放
    trace = call("GET", "/api/batches/RN-B101/handover")[1]
    assert trace["shipped"] is True and trace["handover"]["handover_no"] == hno
    print(f"发运占用保留：RN-B101 仍由交接 {hno} 占用（state=SHIPPED）")

    section("4. 旧号 RN-B1 可查不可写：查询别名、登记/更正拒绝、新清单只认现行号")
    via_old = call("GET", "/api/batches/RN-B1")[1]
    print(f"按旧号查询定位到现行号：batch_no={via_old['batch_no']}，"
          f"历史号={via_old['previous_batch_nos']}")
    assert via_old["batch_no"] == "RN-B101"

    s, body = call("POST", "/api/batches", {
        "batch_no": "RN-B1", "line_id": LINE,
        "material_lots": [], "declared_allergens": [], "cleaned_allergens": [],
    })
    print(f"[{s}] 旧号再登记: {body['error']['code']}")
    assert s == 409 and body["error"]["code"] == "DUPLICATE_BATCH"

    s, body = call("POST", "/api/batches/RN-B1/void", {"reason": "尝试作废历史号"})
    print(f"[{s}] 凭旧号作废: {body['error']['code']}")
    assert s == 409 and body["error"]["code"] == "BATCH_NO_HISTORICAL_ALIAS"

    s, body = call("POST", "/api/verifications",
                   {"verification_no": f"RN-VX-{suffix}", "batch_nos": ["RN-B1"]})
    print(f"[{s}] 新核验清单用旧号: {body['error']['code']}"
          f"（现行号 {body['error']['details'][0]['current_batch_no']}）")
    assert s == 400 and body["error"]["code"] == "BATCH_NO_HISTORICAL_ALIAS"

    section("5. 历史快照保留记录时号；凭旧号处理原交接的退回/复核")
    h = call("GET", f"/api/handovers/{hno}")[1]
    hb = h["batches"][0]
    print(f"交接冻结快照：recorded_batch_no={hb['recorded_batch_no']}，"
          f"current_batch_no={hb['current_batch_no']}，内容版本={hb['adjudication']['version']}")
    assert hb["recorded_batch_no"] == "RN-B1" and hb["current_batch_no"] == "RN-B101"

    snap = doc["confirmations"][0]["batches"][0]
    assert snap["recorded_batch_no"] == "RN-B1" and snap["current_batch_no"] == "RN-B101"

    # 交接后批号锁定 -> 资格版本漂移 -> 发运后复核事项
    s, _ = call("POST", "/api/material-lots/RN-RM-M/lock", {"reason": "供应商通报待检"})
    assert s == 200
    board = call("GET", f"/api/handovers/{hno}/reviews")[1]
    item = board["batches"][0]
    print(f"复核视图：recorded={item['recorded_batch_no']} current={item['current_batch_no']} "
          f"status={item['review_status']}")
    assert item["recorded_batch_no"] == "RN-B1"
    assert item["current_batch_no"] == "RN-B101"
    assert item["review_status"] == "PENDING_REVIEW"

    # 凭【旧号】提交复核处置
    seen = item["seen_state"]
    s, decision = call("POST", f"/api/handovers/{hno}/reviews/RN-B1/decision", {
        "seen_adjudication_version": seen["adjudication_version"],
        "seen_shipment_version": seen["shipment_version"],
        "seen_batch_voided": seen["batch_voided"],
        "disposition": "RECALL_REQUIRED",
        "reason": "锁定批号，已发运产品需召回",
    })
    assert s == 201, decision
    print("凭旧号提交复核处置成功（201）；复核状态 -> REVIEWED")

    # 先解除锁定恢复 SHIPPABLE 状态无关节名；凭【旧号】登记原交接的退回
    s, _ = call("POST", "/api/material-lots/RN-RM-M/unlock", {"reason": "复检合格"})
    assert s == 200
    s, ret = call("POST", f"/api/handovers/{hno}/returns", {
        "return_no": rno, "batch_nos": ["RN-B1"], "reason": "运输途中包装破损",
    })
    assert s == 201, ret
    rb = ret["batches"][0]
    print(f"凭旧号登记退回 {rno}：recorded_batch_no={rb['recorded_batch_no']}，"
          f"current_batch_no={rb['current_batch_no']}")
    assert rb["recorded_batch_no"] == "RN-B1" and rb["batch_no"] == "RN-B101"
    # 占用已释放（编号更正不释放占用，但退回释放本批）
    assert call("GET", "/api/batches/RN-B1/handover")[1]["shipped"] is False

    section("6. 连续更正 RN-B101 -> RN-B102：历次旧号链都保留、都可追溯")
    s, body = rename("RN-B101", "RN-B102", "再次核对后按正确编号更正")
    assert s == 200, body
    assert body["identity"]["previous_batch_nos"] == ["RN-B1", "RN-B101"]
    for no in ("RN-B1", "RN-B101", "RN-B102"):
        b = call("GET", f"/api/batches/{no}")[1]
        assert b["batch_no"] == "RN-B102"
    print("RN-B1 / RN-B101 / RN-B102 三个号都定位到同一现行批 RN-B102")
    # 交接归属用任一历史号都可追溯，冻结的记录时号始终是 RN-B1
    trace = call("GET", "/api/batches/RN-B101/handover")[1]
    assert trace["handovers"][0]["recorded_batch_no"] == "RN-B1"
    assert trace["handovers"][0]["current_batch_no"] == "RN-B102"

    section("7. 只追加的编号更正记录")
    s, all_records = call("GET", "/api/batch-renames")
    show("GET /api/batch-renames", s, all_records)
    rows = [r for r in all_records["renames"]
            if r["previous_batch_no"] in ("RN-B1", "RN-B101")]
    assert [(r["previous_batch_no"], r["new_batch_no"]) for r in rows] == [
        ("RN-B1", "RN-B101"), ("RN-B101", "RN-B102"),
    ]
    filtered = call("GET", "/api/batch-renames?batch_no=RN-B1")[1]
    assert filtered["current_batch_no"] == "RN-B102"
    assert len(filtered["renames"]) == 2

    section("完成")
    print("批次号 RN-B1 → RN-B101 → RN-B102 连续更正：历次旧号永久保留为查询别名、")
    print("不得再登记；裁决/资格版本、单据修订号与状态不变、发运占用不因更名释放；")
    print("确认/交接/退回/复核历史快照保留记录时号，并同时返回历史号与现行号。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

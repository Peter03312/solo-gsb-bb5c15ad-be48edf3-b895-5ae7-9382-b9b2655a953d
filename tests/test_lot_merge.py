"""原料批号并档测试。

覆盖：
- 基本并档：源批号留档停用（status=MERGED、并档目标/原因/时间可查），
  目标批号保留自身名称/过敏原/待检锁定状态；并档记录只追加可查；
- 有效批次直接用料改指目标号（原本同时引用两号的批次去重），
  受影响产线从最早引用批次起按序重算并追加裁决版本（trigger=MERGE_LOT）；
- 已作废批次登记/历史不改写，不参与改指与重算；
- 出货资格按目标号当前锁定状态更新：源锁定→目标未锁定转 SHIPPABLE、
  源未锁定→目标锁定转 BLOCKED、两侧均未锁定不递增资格版本；
- 已确认核验单随版本变化转待复核（草稿不转态），历次确认快照不改；
- 已发运批次仍按原交接追溯：交接记录与逐批冻结快照不改写；
- 源号停用后不再接受新批次引用（登记/更正 400）、更正（409）、锁定/解除（409）、
  再次并档或作为并档目标（409）；
- 拒绝：相同编号 409、未知源/目标批号 404、空原因/空目标 400；
  失败整体回滚，不留下部分引用或版本变更；
- 旧库升级：material_lot 新列与并档表幂等补齐，既有批号视为 ACTIVE。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def merge(client, source, target, reason="同一原料误登记为两个编号，并档"):
    return client.post(
        f"/api/material-lots/{source}/merge",
        json={"target_lot_no": target, "reason": reason},
    )


def lock(client, lot_no, reason="待检"):
    return client.post(f"/api/material-lots/{lot_no}/lock", json={"reason": reason})


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


def confirm(client, verification_no):
    return client.post(f"/api/verifications/{verification_no}/confirm")


def get_batch(client, batch_no):
    resp = client.get(f"/api/batches/{batch_no}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- 基本并档与留档


def test_merge_retires_source_and_keeps_target_identity(client):
    add_lot(client, "MG-S", "花生酱（误登记甲）", ["peanut"])
    add_lot(client, "MG-T", "花生酱", ["peanut", "sesame"])
    lock(client, "MG-T", "目标批号待检")

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # 并档记录
    assert body["merge"]["source_lot_no"] == "MG-S"
    assert body["merge"]["target_lot_no"] == "MG-T"
    assert body["merge"]["reason"]
    assert body["merge"]["merged_at"]
    assert body["merge"]["merge_id"] >= 1

    # 源批号留档停用
    src = body["source_lot"]
    assert src["status"] == "MERGED"
    assert src["merged"] is True
    assert src["merged_into"] == "MG-T"
    assert src["merged_reason"]
    assert src["merged_at"]
    assert src["material"] == "花生酱（误登记甲）"  # 原登记保留可查
    assert src["allergens"] == ["peanut"]

    # 目标批号保留自身名称、过敏原与待检锁定状态
    tgt = body["target_lot"]
    assert tgt["status"] == "ACTIVE"
    assert tgt["merged"] is False
    assert tgt["material"] == "花生酱"
    assert tgt["allergens"] == ["peanut", "sesame"]
    assert tgt["lock"]["locked"] is True

    # 源批号仍可查询（留档）：详情与锁定状态接口正常
    got = client.get("/api/material-lots/MG-S")
    assert got.status_code == 200
    assert got.json()["status"] == "MERGED"
    assert got.json()["merged_into"] == "MG-T"
    assert client.get("/api/material-lots/MG-S/lock").status_code == 200
    # 列表同样标明
    lots = {l["lot_no"]: l for l in client.get("/api/material-lots").json()["material_lots"]}
    assert lots["MG-S"]["status"] == "MERGED"
    assert lots["MG-T"]["status"] == "ACTIVE"

    # 并档记录只追加可查
    merges = client.get("/api/material-lot-merges").json()["merges"]
    assert len(merges) == 1
    assert merges[0]["source_lot_no"] == "MG-S"
    assert merges[0]["target_lot_no"] == "MG-T"


def test_merge_without_any_reference_still_retires_source(client):
    add_lot(client, "MG-S", "奶粉甲", ["milk"])
    add_lot(client, "MG-T", "奶粉", ["milk"])

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["redirected_batches"] == []
    assert body["recomputed"] == []
    assert body["shipment_eligibility"] == []
    assert body["affected_verifications"] == []
    assert body["source_lot"]["status"] == "MERGED"


# ---------------------------------------------------------------- 改指、去重与重算


def test_active_batches_redirected_and_lines_recomputed_from_earliest(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["sesame"])
    add_batch(client, "B0", lots=[], declared=[])                       # 不引用源号，不受影响
    add_batch(client, "B1", lots=["MG-S"], declared=["peanut"])         # RELEASE，残留 peanut
    add_batch(client, "B2", lots=[], declared=[])                       # 继承 peanut -> HOLD
    add_batch(client, "C1", line_id="L2", lots=["MG-S"], declared=["peanut"])

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # 改指：有效批次用料改指目标号
    redirected = {r["batch_no"]: r for r in body["redirected_batches"]}
    assert set(redirected) == {"B1", "C1"}
    assert redirected["B1"]["from_material_lots"] == ["MG-S"]
    assert redirected["B1"]["to_material_lots"] == ["MG-T"]
    assert redirected["B1"]["deduplicated"] is False
    assert get_batch(client, "B1")["material_lots"] == ["MG-T"]
    assert get_batch(client, "C1")["material_lots"] == ["MG-T"]

    # 两条产线各自从最早引用批次起重算（B0 在引用点之前，不重算）
    recomputed = {(s["batch_no"]): s for s in body["recomputed"]}
    assert set(recomputed) == {"B1", "B2", "C1"}
    assert recomputed["B1"]["version"] == 2
    assert recomputed["B2"]["version"] == 2
    assert recomputed["C1"]["version"] == 2
    # B1 改用 sesame 目标号：sesame 未声明 -> HOLD
    assert recomputed["B1"]["actual"] == ["sesame"]
    assert recomputed["B1"]["disposition"] == "HOLD"
    # B2 继承 B1 的 sesame 残留
    assert recomputed["B2"]["actual"] == ["sesame"]
    assert get_batch(client, "B0")["adjudication"]["version"] == 1

    # 历史版本只追加：v1 原样保留，v2 trigger=MERGE_LOT
    hist = client.get("/api/batches/B1/history").json()["history"]
    assert [v["version"] for v in hist] == [1, 2]
    assert hist[0]["trigger"] == "REGISTER"
    assert hist[1]["trigger"] == "MERGE_LOT"


def test_batch_referencing_both_lots_is_deduplicated(client):
    add_lot(client, "MG-S", "奶粉甲", ["milk"])
    add_lot(client, "MG-T", "奶粉乙", ["milk"])
    add_batch(client, "B1", lots=["MG-S", "MG-T"], declared=["milk"])
    add_batch(client, "B2", lots=["MG-T", "MG-S"], declared=["milk"])

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    redirected = {r["batch_no"]: r for r in resp.json()["redirected_batches"]}
    assert redirected["B1"]["to_material_lots"] == ["MG-T"]
    assert redirected["B1"]["deduplicated"] is True
    assert redirected["B2"]["to_material_lots"] == ["MG-T"]
    assert redirected["B2"]["deduplicated"] is True
    assert get_batch(client, "B1")["material_lots"] == ["MG-T"]
    assert get_batch(client, "B2")["material_lots"] == ["MG-T"]


def test_voided_batch_not_rewritten(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])
    add_batch(client, "B1", lots=["MG-S"], declared=["peanut"])
    add_batch(client, "B2", lots=["MG-S"], declared=["peanut"])
    client.post("/api/batches/B1/void", json={"reason": "误登记未投产"})

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # 作废批次不改指、不重算
    assert [r["batch_no"] for r in body["redirected_batches"]] == ["B2"]
    assert [s["batch_no"] for s in body["recomputed"]] == ["B2"]
    voided = get_batch(client, "B1")
    assert voided["status"] == "VOIDED"
    assert voided["material_lots"] == ["MG-S"]          # 登记内容不改写
    assert voided["adjudication"]["version"] == 1        # 冻结，不追加版本
    hist = client.get("/api/batches/B1/history").json()["history"]
    assert len(hist) == 1


# ---------------------------------------------------------------- 出货资格按目标号锁定状态更新


def test_eligibility_updated_to_target_lock_state_unlocked_target(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])
    add_batch(client, "B1", lots=["MG-S"], declared=["peanut"])
    lock(client, "MG-S", "源批号待检")
    assert get_batch(client, "B1")["shipment_eligibility"]["status"] == "BLOCKED"
    assert get_batch(client, "B1")["shipment_eligibility"]["version"] == 2

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    changes = resp.json()["shipment_eligibility"]
    assert [c["batch_no"] for c in changes] == ["B1"]
    assert changes[0]["status"] == "SHIPPABLE"
    assert changes[0]["locked_lots"] == []
    assert changes[0]["version"] == 3

    elig = client.get("/api/batches/B1/eligibility").json()
    assert elig["eligibility"]["status"] == "SHIPPABLE"
    assert [v["trigger"] for v in elig["history"]] == ["REGISTER", "LOT_LOCK", "MERGE_LOT"]


def test_eligibility_updated_to_target_lock_state_locked_target(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])
    add_batch(client, "B1", lots=["MG-S"], declared=["peanut"])
    lock(client, "MG-T", "目标批号待检")

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    changes = resp.json()["shipment_eligibility"]
    assert [c["batch_no"] for c in changes] == ["B1"]
    assert changes[0]["status"] == "BLOCKED"
    assert changes[0]["locked_lots"] == ["MG-T"]
    assert changes[0]["version"] == 2
    elig = client.get("/api/batches/B1/eligibility").json()
    assert elig["eligibility"]["locked_lots"] == ["MG-T"]
    assert elig["history"][-1]["trigger"] == "MERGE_LOT"


def test_eligibility_not_advanced_when_lock_state_equivalent(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])
    add_batch(client, "B1", lots=["MG-S"], declared=["peanut"])

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    # 两侧均未锁定：资格无实际变化，不递增版本
    assert resp.json()["shipment_eligibility"] == []
    elig = client.get("/api/batches/B1/eligibility").json()
    assert elig["eligibility"]["version"] == 1
    assert [v["trigger"] for v in elig["history"]] == ["REGISTER"]


# ---------------------------------------------------------------- 核验单联动


def test_confirmed_verification_flips_to_pending_review(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])
    add_batch(client, "B1", lots=["MG-S"], declared=["peanut"])
    create_verification(client, "V-DRAFT", ["B1"])
    create_verification(client, "V-CONF", ["B1"])
    confirm(client, "V-CONF")

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    affected = {v["verification_no"]: v["status"] for v in resp.json()["affected_verifications"]}
    assert affected == {"V-DRAFT": "DRAFT", "V-CONF": "PENDING_REVIEW"}

    doc = client.get("/api/verifications/V-CONF").json()
    assert doc["status"] == "PENDING_REVIEW"
    assert doc["batches"][0]["changed"] is True
    assert doc["batches"][0]["change_reasons"][-1]["trigger"] == "MERGE_LOT"
    # 历次确认快照不改写
    assert len(doc["confirmations"]) == 1
    assert doc["confirmations"][0]["batches"][0]["version"] == 1

    # 重新核验后可再次确认
    client.post("/api/verifications/V-CONF/reverify")
    resp = confirm(client, "V-CONF")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["confirmations"]) == 2


def test_confirmed_verification_flips_on_eligibility_change(client):
    """即使裁决结论不变，出货资格版本变化也使已确认核验单转待复核。"""
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])
    add_batch(client, "B1", lots=["MG-S"], declared=["peanut"])
    lock(client, "MG-T", "目标批号待检")
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text
    assert client.get("/api/verifications/V1").json()["status"] == "PENDING_REVIEW"
    # 直接确认被拒：出货资格快照陈旧 + 当前锁定
    resp = confirm(client, "V1")
    assert resp.status_code == 422
    codes = {d["reason_code"] for d in resp.json()["error"]["details"]}
    assert "STALE_ELIGIBILITY" in codes
    assert "MATERIAL_LOT_LOCKED" in codes


# ---------------------------------------------------------------- 已发运批次仍按原交接追溯


def test_shipped_batch_handover_records_untouched(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])
    add_batch(client, "B1", lots=["MG-S"], declared=["peanut"])
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")
    revision = client.get("/api/verifications/V1").json()["revision"]
    resp = client.post(
        "/api/handovers",
        json={"handover_no": "HO1", "verification_no": "V1", "expected_revision": revision},
    )
    assert resp.status_code == 201, resp.text
    before = client.get("/api/handovers/HO1").json()

    resp = merge(client, "MG-S", "MG-T")
    assert resp.status_code == 201, resp.text

    # 交接记录与逐批冻结快照不改写；批次仍按原交接追溯
    after = client.get("/api/handovers/HO1").json()
    assert after == before
    where = client.get("/api/batches/B1/handover").json()
    assert where["shipped"] is True
    assert where["handover"]["handover_no"] == "HO1"
    # 发运后复核视图能观察到并档带来的版本变化（交接快照本身不变）
    reviews = client.get("/api/handovers/HO1/reviews").json()
    assert reviews["batches"][0]["review_status"] == "PENDING_REVIEW"
    assert reviews["batches"][0]["handover_snapshot"]["adjudication"]["version"] == 1


# ---------------------------------------------------------------- 源号停用后的操作拒绝


def test_merged_source_rejected_everywhere(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])
    add_lot(client, "MG-X", "其他原料", [])
    add_batch(client, "B1", lots=["MG-X"], declared=[])
    merge(client, "MG-S", "MG-T")

    # 新批次引用已停用批号 -> 400
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": "B2", "line_id": "L1",
            "material_lots": ["MG-S"], "declared_allergens": [], "cleaned_allergens": [],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"
    assert resp.json()["error"]["details"][0]["merged_into"] == "MG-T"

    # 更正批次用料引入已停用批号 -> 400
    resp = client.put("/api/batches/B1", json={"material_lots": ["MG-X", "MG-S"]})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"
    assert get_batch(client, "B1")["material_lots"] == ["MG-X"]  # 未留下部分更正

    # 更正已停用批号 -> 409
    resp = client.put("/api/material-lots/MG-S", json={"allergens": ["milk"]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"

    # 锁定/解除已停用批号 -> 409
    for path in ("lock", "unlock"):
        resp = client.post(f"/api/material-lots/MG-S/{path}", json={"reason": "待检"})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"

    # 已停用批号不能再次并档，也不能作为并档目标
    resp = merge(client, "MG-S", "MG-X")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"
    resp = merge(client, "MG-X", "MG-S")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_MERGED"

    # 已停用批号号不可复用
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": "MG-S", "material": "重新登记", "allergens": []},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_MATERIAL_LOT"


# ---------------------------------------------------------------- 拒绝与原子性


def test_merge_rejections(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])

    # 相同编号 -> 409
    resp = merge(client, "MG-S", "MG-S")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SAME_SOURCE_TARGET_LOT"

    # 未知源/目标批号 -> 404
    resp = merge(client, "NO-SUCH", "MG-T")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_NOT_FOUND"
    resp = merge(client, "MG-S", "NO-SUCH")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "MATERIAL_LOT_NOT_FOUND"

    # 空原因 / 纯空白原因 -> 400
    for reason in ("", "   "):
        resp = merge(client, "MG-S", "MG-T", reason=reason)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"

    # 空目标批号 -> 400
    resp = merge(client, "MG-S", "   ")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"

    # 全部拒绝后：两号仍可使用，无并档记录
    lots = {l["lot_no"]: l for l in client.get("/api/material-lots").json()["material_lots"]}
    assert lots["MG-S"]["status"] == "ACTIVE"
    assert lots["MG-T"]["status"] == "ACTIVE"
    assert client.get("/api/material-lot-merges").json()["merges"] == []


def test_failed_merge_leaves_no_partial_changes(client):
    add_lot(client, "MG-S", "花生酱甲", ["peanut"])
    add_lot(client, "MG-T", "花生酱乙", ["peanut"])
    add_batch(client, "B1", lots=["MG-S"], declared=["peanut"])
    create_verification(client, "V1", ["B1"])
    confirm(client, "V1")

    # 未知目标批号：整体拒绝
    resp = merge(client, "MG-S", "NO-SUCH")
    assert resp.status_code == 404

    # 不留下部分引用或版本变更
    b1 = get_batch(client, "B1")
    assert b1["material_lots"] == ["MG-S"]
    assert b1["adjudication"]["version"] == 1
    assert b1["shipment_eligibility"]["version"] == 1
    assert client.get("/api/material-lots/MG-S").json()["status"] == "ACTIVE"
    assert client.get("/api/verifications/V1").json()["status"] == "CONFIRMED"
    assert client.get("/api/material-lot-merges").json()["merges"] == []

    # 空原因同样整体拒绝
    resp = merge(client, "MG-S", "MG-T", reason=" ")
    assert resp.status_code == 400
    assert get_batch(client, "B1")["material_lots"] == ["MG-S"]
    assert client.get("/api/material-lots/MG-S").json()["status"] == "ACTIVE"


# ---------------------------------------------------------------- 旧库升级


def test_database_upgrade_adds_merge_columns_and_table(tmp_path):
    """旧版本库（material_lot 无并档列、无并档表）启动后自动升级，既有批号为 ACTIVE。"""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE allergen (code TEXT PRIMARY KEY, name_zh TEXT NOT NULL);
        CREATE TABLE material_lot (lot_no TEXT PRIMARY KEY, material TEXT NOT NULL,
            allergens TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        INSERT INTO allergen(code, name_zh) VALUES ('peanut', '花生');
        INSERT INTO material_lot VALUES ('RM-A', '花生酱甲', '["peanut"]', 't');
        INSERT INTO material_lot VALUES ('RM-B', '花生酱乙', '["peanut"]', 't');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        lots = {l["lot_no"]: l for l in c.get("/api/material-lots").json()["material_lots"]}
        assert lots["RM-A"]["status"] == "ACTIVE"
        assert lots["RM-A"]["merged"] is False
        assert lots["RM-A"]["merged_into"] is None

        # 升级后并档可用
        resp = c.post(
            "/api/material-lots/RM-A/merge",
            json={"target_lot_no": "RM-B", "reason": "同一原料误登记两个编号"},
        )
        assert resp.status_code == 201, resp.text
        assert c.get("/api/material-lots/RM-A").json()["status"] == "MERGED"
        merges = c.get("/api/material-lot-merges").json()["merges"]
        assert len(merges) == 1
        assert merges[0]["source_lot_no"] == "RM-A"

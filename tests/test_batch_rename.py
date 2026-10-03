"""已投产批次号更正（标识更正）测试。

覆盖：
- 基础更名：凭现行号、未占用新号、非空原因更正；同批连续更正；
  历次旧号永久保留为查询别名、不得再登记，也不得凭旧号更正/作废/清洁更正；
- 只改标识：产线/顺序号、用料/声明/清洁、当前裁决/出货资格及其版本、
  核验单修订号与状态均不变；不追加裁决/资格版本；发运占用不释放；
- 当前关联：批次、裁决、资格、核验单当前成员、占用均以现行号关联同一批；
- 历史快照：已确认核验快照、交接冻结快照、退回、发运后复核保留记录时批次号，
  逐批同时返回 recorded_batch_no（历史号）与 current_batch_no（现行号）；
- 按旧号查询/处理：批次详情/历史/资格/交接归属/复核视图可凭旧号定位，
  原交接的退回与复核处置可凭旧号提交；新核验清单只接受现行号；
- 拒绝：未知或已作废批次、空原因/空目标号、目标号与现行号相同、
  目标号已被现行号或历史别名占用（含本批旧号）；失败原子回滚，不留部分更名或引用；
- 旧库升级：别名表与 recorded_batch_no 列幂等回填。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no="RM-M", material="奶粉", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or ["milk"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots if lots is not None else ["RM-M"],
            "declared_allergens": ["milk"] if declared is None else declared,
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def rename(client, batch_no, new_batch_no, reason="现场发现批次号录错，更正标识"):
    return client.post(
        f"/api/batches/{batch_no}/rename",
        json={"new_batch_no": new_batch_no, "reason": reason},
    )


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


def confirm(client, verification_no):
    return client.post(f"/api/verifications/{verification_no}/confirm")


def make_handover(client, batches, verification_no="V1", handover_no="H1"):
    assert create_verification(client, verification_no, batches).status_code == 201
    assert confirm(client, verification_no).status_code == 200
    rev = client.get(f"/api/verifications/{verification_no}").json()["revision"]
    resp = client.post(
        "/api/handovers",
        json={
            "handover_no": handover_no,
            "verification_no": verification_no,
            "expected_revision": rev,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------- 基础更名与别名


def test_rename_basic_current_no_and_aliases(client):
    add_lot(client)
    add_batch(client, "B1")

    resp = rename(client, "B1", "B1-NEW")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["rename"] == {
        "rename_id": 1,
        "previous_batch_no": "B1",
        "new_batch_no": "B1-NEW",
        "reason": "现场发现批次号录错，更正标识",
        "renamed_at": body["rename"]["renamed_at"],
    }
    assert body["renamed"]["batch_no"] == "B1-NEW"
    assert body["identity"] == {
        "current_batch_no": "B1-NEW",
        "previous_batch_nos": ["B1"],
        "alias_batch_nos": ["B1"],
    }

    # 现行号可查
    cur = client.get("/api/batches/B1-NEW").json()
    assert cur["batch_no"] == "B1-NEW"
    assert cur["current_batch_no"] == "B1-NEW"
    assert cur["previous_batch_nos"] == ["B1"]
    # 历史号仍可查询，定位同一批并同时返回历史号与现行号
    old = client.get("/api/batches/B1").json()
    assert old["batch_no"] == "B1-NEW"
    assert old["previous_batch_nos"] == ["B1"]


def test_rename_can_be_applied_repeatedly_with_full_alias_chain(client):
    add_lot(client)
    add_batch(client, "B1")
    assert rename(client, "B1", "B2").status_code == 200
    resp = rename(client, "B2", "B3")
    assert resp.status_code == 200, resp.text
    assert resp.json()["identity"]["previous_batch_nos"] == ["B1", "B2"]

    # 三个号都定位到同一现行批
    for no in ("B1", "B2", "B3"):
        assert client.get(f"/api/batches/{no}").json()["batch_no"] == "B3"

    # 历次旧号都不得再登记
    for no in ("B1", "B2"):
        resp = client.post(
            "/api/batches",
            json={
                "batch_no": no, "line_id": "L9",
                "material_lots": [], "declared_allergens": [], "cleaned_allergens": [],
            },
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "DUPLICATE_BATCH"


def test_old_alias_rejected_for_write_operations(client):
    add_lot(client)
    add_batch(client, "B1")
    assert rename(client, "B1", "B1-NEW").status_code == 200

    # 凭历史号更正批次内容
    resp = client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_NO_HISTORICAL_ALIAS"

    # 清洁更正
    resp = client.put("/api/batches/B1/cleaning", json={"cleaned_allergens": ["milk"]})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_NO_HISTORICAL_ALIAS"

    # 清洁预演
    resp = client.post(
        "/api/batches/B1/cleaning/preview", json={"cleaned_allergens": ["milk"]}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_NO_HISTORICAL_ALIAS"

    # 作废
    resp = client.post("/api/batches/B1/void", json={"reason": "误登记"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_NO_HISTORICAL_ALIAS"

    # 换线
    resp = client.post("/api/batches/B1/reassign", json={"target_line_id": "L2"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_NO_HISTORICAL_ALIAS"

    # 用现行号作废正常
    assert client.post("/api/batches/B1-NEW/void", json={"reason": "误登记"}).status_code == 200


def test_rename_history_endpoint(client):
    add_lot(client)
    add_batch(client, "B1")
    assert rename(client, "B1", "B2").status_code == 200
    assert rename(client, "B2", "B3").status_code == 200

    # 全量
    body = client.get("/api/batch-renames").json()
    assert [r["previous_batch_no"] for r in body["renames"]] == ["B1", "B2"]
    assert [r["new_batch_no"] for r in body["renames"]] == ["B2", "B3"]

    # 按历史号过滤同一批
    body = client.get("/api/batch-renames?batch_no=B1").json()
    assert body["current_batch_no"] == "B3"
    assert body["queried_batch_no"] == "B1"
    assert [r["new_batch_no"] for r in body["renames"]] == ["B2", "B3"]

    # 按现行号
    assert len(client.get("/api/batch-renames?batch_no=B3").json()["renames"]) == 2

    # 未知号
    resp = client.get("/api/batch-renames?batch_no=NOPE")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_FOUND"


# ---------------------------------------------------------------- 只改标识：版本/状态/占用不变


def test_rename_keeps_line_seq_content_and_versions(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    b = add_batch(client, "B1", lots=["RM-P"], declared=["peanut", "milk"])
    assert b["adjudication"]["version"] == 1
    assert b["shipment_eligibility"]["version"] == 1

    resp = rename(client, "B1", "B1-NEW")
    assert resp.status_code == 200
    renamed = resp.json()["renamed"]
    # 产线/顺序号/登记内容不变
    assert renamed["line_id"] == "L1" and renamed["seq"] == 1
    assert renamed["material_lots"] == ["RM-P"]
    assert renamed["declared_allergens"] == ["peanut", "milk"]
    # 当前裁决/资格版本不变、不追加历史版本
    assert renamed["adjudication"]["version"] == 1
    assert renamed["shipment_eligibility"]["version"] == 1
    hist = client.get("/api/batches/B1-NEW/history").json()
    assert [v["version"] for v in hist["history"]] == [1]
    assert hist["history"][0]["trigger"] == "REGISTER"
    elig = client.get("/api/batches/B1-NEW/eligibility").json()
    assert [v["trigger"] for v in elig["history"]] == ["REGISTER"]


def test_rename_does_not_change_verification_revision_or_status(client):
    add_lot(client)
    add_batch(client, "B1")
    assert create_verification(client, "SH-1", ["B1"]).status_code == 201
    assert confirm(client, "SH-1").status_code == 200
    rev_before = client.get("/api/verifications/SH-1").json()["revision"]

    assert rename(client, "B1", "B1-NEW").status_code == 200

    doc = client.get("/api/verifications/SH-1").json()
    assert doc["status"] == "CONFIRMED"          # 不转待复核
    assert doc["revision"] == rev_before          # 修订号不变
    rec = doc["batches"][0]
    assert rec["batch_no"] == "B1-NEW"
    assert rec["current_batch_no"] == "B1-NEW"
    assert rec["previous_batch_nos"] == ["B1"]
    assert rec["changed"] is False                # 裁决版本未漂移
    assert rec["shipment_changed"] is False


def test_rename_does_not_release_shipment_occupancy(client):
    add_lot(client)
    add_batch(client, "B1")
    make_handover(client, ["B1"], verification_no="V1", handover_no="H1")

    assert rename(client, "B1", "B1-NEW").status_code == 200

    # 占用随批更名、仍占用：可凭旧号或现行号追溯到原交接
    for no in ("B1", "B1-NEW"):
        trace = client.get(f"/api/batches/{no}/handover").json()
        assert trace["shipped"] is True
        assert trace["current_whereabouts"]["state"] == "SHIPPED"
        assert trace["handover"]["handover_no"] == "H1"

    # 新核验单可纳入现行号（但交接仍会因占用唯一而拒绝）
    assert create_verification(client, "V2", ["B1-NEW"]).status_code == 201
    assert confirm(client, "V2").status_code == 200
    rev = client.get("/api/verifications/V2").json()["revision"]
    resp = client.post(
        "/api/handovers",
        json={"handover_no": "H2", "verification_no": "V2", "expected_revision": rev},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_ALREADY_SHIPPED"


# ---------------------------------------------------------------- 历史快照保留记录时号


def test_confirmation_snapshot_keeps_recorded_batch_no(client):
    add_lot(client)
    add_batch(client, "B1")
    assert create_verification(client, "SH-1", ["B1"]).status_code == 201
    assert confirm(client, "SH-1").status_code == 200

    assert rename(client, "B1", "B1-NEW").status_code == 200

    doc = client.get("/api/verifications/SH-1").json()
    snap = doc["confirmations"][0]["batches"][0]
    assert snap["recorded_batch_no"] == "B1"      # 历史快照保留记录时号
    assert snap["current_batch_no"] == "B1-NEW"   # 同时回传现行号
    assert snap["batch_no"] == "B1-NEW"


def test_handover_snapshot_keeps_recorded_batch_no(client):
    add_lot(client)
    add_batch(client, "B1")
    make_handover(client, ["B1"], verification_no="V1", handover_no="H1")
    assert rename(client, "B1", "B1-NEW").status_code == 200

    h = client.get("/api/handovers/H1").json()
    b = h["batches"][0]
    assert b["recorded_batch_no"] == "B1"
    assert b["current_batch_no"] == "B1-NEW"
    assert b["batch_no"] == "B1-NEW"


def test_return_of_original_handover_can_use_old_no(client):
    add_lot(client)
    add_batch(client, "B1")
    make_handover(client, ["B1"], verification_no="V1", handover_no="H1")
    assert rename(client, "B1", "B1-NEW").status_code == 200

    # 凭记录时批次号（旧号）登记原交接的退回
    resp = client.post(
        "/api/handovers/H1/returns",
        json={"return_no": "RT-1", "batch_nos": ["B1"], "reason": "包装破损"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    item = body["batches"][0]
    assert item["batch_no"] == "B1-NEW"
    assert item["recorded_batch_no"] == "B1"

    # 占用已释放（按现行号）
    trace = client.get("/api/batches/B1-NEW/handover").json()
    assert trace["shipped"] is False
    assert trace["current_whereabouts"]["state"] == "AVAILABLE"

    # 原交接详情仍显示当时清单，并标注退回；历史号/现行号同时返回
    h = client.get("/api/handovers/H1").json()
    hb = h["batches"][0]
    assert hb["recorded_batch_no"] == "B1" and hb["current_batch_no"] == "B1-NEW"
    assert hb["returned"] is True
    assert hb["return"]["return_no"] == "RT-1"


def test_post_shipment_review_reachable_by_old_no(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    make_handover(client, ["B1"], verification_no="V1", handover_no="H1")
    # 交接后锁定批号 -> 出货资格版本漂移，形成复核事项
    assert client.post(
        "/api/material-lots/RM-P/lock", json={"reason": "待检"}
    ).status_code == 200
    assert rename(client, "B1", "B1-NEW").status_code == 200

    # 复核视图可凭交接查：逐批同时给出历史号与现行号
    board = client.get("/api/handovers/H1/reviews").json()
    item = board["batches"][0]
    assert item["recorded_batch_no"] == "B1"
    assert item["current_batch_no"] == "B1-NEW"
    assert item["batch_no"] == "B1-NEW"
    assert item["review_status"] == "PENDING_REVIEW"
    seen = item["seen_state"]

    # 凭旧号提交处置
    resp = client.post(
        "/api/handovers/H1/reviews/B1/decision",
        json={
            "seen_adjudication_version": seen["adjudication_version"],
            "seen_shipment_version": seen["shipment_version"],
            "seen_batch_voided": seen["batch_voided"],
            "disposition": "RECALL_REQUIRED",
            "reason": "锁定批号，需召回",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["batch"]["current_batch_no"] == "B1-NEW"
    # 复核视图转为已复核
    board = client.get("/api/handovers/H1/reviews").json()
    assert board["batches"][0]["review_status"] == "REVIEWED"


def test_new_verification_only_accepts_current_no(client):
    add_lot(client)
    add_batch(client, "B1")
    assert rename(client, "B1", "B1-NEW").status_code == 200

    resp = create_verification(client, "SH-1", ["B1"])
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "BATCH_NO_HISTORICAL_ALIAS"
    assert err["details"][0]["current_batch_no"] == "B1-NEW"

    # 现行号正常
    assert create_verification(client, "SH-2", ["B1-NEW"]).status_code == 201

    # 修订清单同样拒绝旧号
    assert client.post(
        "/api/verifications/SH-2/revise",
        json={"expected_revision": 1, "batch_nos": ["B1"]},
    ).status_code == 400


# ---------------------------------------------------------------- 拒绝与原子性


def test_reject_unknown_batch(client):
    resp = rename(client, "NOPE", "X1")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "BATCH_NOT_FOUND"


def test_reject_voided_batch_rename(client):
    add_lot(client)
    add_batch(client, "B1")
    assert client.post("/api/batches/B1/void", json={"reason": "误登记"}).status_code == 200
    resp = rename(client, "B1", "B1-NEW")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"


def test_reject_blank_reason_and_blank_target(client):
    add_lot(client)
    add_batch(client, "B1")
    resp = client.post(
        "/api/batches/B1/rename", json={"new_batch_no": "B2", "reason": "   "}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"
    resp = client.post(
        "/api/batches/B1/rename", json={"new_batch_no": "   ", "reason": "录错"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"


def test_missing_fields_are_422(client):
    add_lot(client)
    add_batch(client, "B1")
    assert client.post("/api/batches/B1/rename", json={"reason": "x"}).status_code == 422
    assert client.post("/api/batches/B1/rename", json={"new_batch_no": "B2"}).status_code == 422


def test_reject_same_current_no(client):
    add_lot(client)
    add_batch(client, "B1")
    resp = rename(client, "B1", "B1")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SAME_SOURCE_TARGET_BATCH_NO"


def test_reject_target_taken_by_current_no(client):
    add_lot(client)
    add_batch(client, "B1")
    add_batch(client, "B2")
    resp = rename(client, "B1", "B2")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "BATCH_NO_ALREADY_TAKEN"
    assert err["details"][0]["occupied_as"] == "CURRENT"


def test_reject_target_taken_by_historical_alias(client):
    add_lot(client)
    add_batch(client, "B1")
    add_batch(client, "B9")
    assert rename(client, "B1", "B1-NEW").status_code == 200

    # 另一批想改成 B1（已是他批历史别名）
    resp = rename(client, "B9", "B1")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "BATCH_NO_ALREADY_TAKEN"
    assert err["details"][0]["occupied_as"] == "HISTORICAL_ALIAS"
    assert err["details"][0]["occupied_by_current_batch_no"] == "B1-NEW"

    # 本批也不能改回自己的旧号
    resp = rename(client, "B1-NEW", "B1")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BATCH_NO_ALREADY_TAKEN"


def test_failure_is_atomic_no_partial_rename(client):
    add_lot(client)
    add_batch(client, "B1")
    add_batch(client, "B2")

    # 目标号被占用 -> 不存在任何部分更名：无更正记录、无别名、现行号不变
    resp = rename(client, "B1", "B2")
    assert resp.status_code == 409

    assert client.get("/api/batches/B1").json()["batch_no"] == "B1"
    assert client.get("/api/batches/B1").json()["previous_batch_nos"] == []
    assert client.get("/api/batch-renames").json()["renames"] == []
    # B1 仍可正常再更名到空闲号
    assert rename(client, "B1", "B3").status_code == 200


def test_rename_then_normal_correction_uses_current_no(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=[])
    assert rename(client, "B1", "B1-NEW").status_code == 200

    # 现行号下更正用料/声明，正常重算
    resp = client.put(
        "/api/batches/B1-NEW", json={"declared_allergens": ["peanut"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["corrected"]["adjudication"]["disposition"] == "RELEASE"


def test_rename_chain_aliases_queryable_in_all_snapshot_views(client):
    add_lot(client)
    add_batch(client, "B1")
    make_handover(client, ["B1"], verification_no="V1", handover_no="H1")
    assert rename(client, "B1", "B2").status_code == 200
    assert rename(client, "B2", "B3").status_code == 200

    # 中间号与最早号都能追溯交接
    for no in ("B1", "B2", "B3"):
        trace = client.get(f"/api/batches/{no}/handover").json()
        assert trace["shipped"] is True
        assert trace["handovers"][0]["recorded_batch_no"] == "B1"
        assert trace["handovers"][0]["current_batch_no"] == "B3"

    h = client.get("/api/handovers/H1").json()
    assert h["batches"][0]["recorded_batch_no"] == "B1"
    assert h["batches"][0]["current_batch_no"] == "B3"


# ---------------------------------------------------------------- 旧库升级


def test_legacy_database_upgrade_backfills_aliases_and_recorded_nos(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # 用最小的旧结构手工造一条已交接数据（无 batch_no_alias / batch_no_rename 表，
    # 冻结表无 recorded_batch_no 列）
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE allergen(code TEXT PRIMARY KEY, name_zh TEXT);
        CREATE TABLE material_lot(lot_no TEXT PRIMARY KEY, material TEXT NOT NULL,
            allergens TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE line(line_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE batch(batch_no TEXT PRIMARY KEY, line_id TEXT NOT NULL, seq INTEGER NOT NULL,
            material_lots TEXT NOT NULL DEFAULT '[]', declared TEXT NOT NULL DEFAULT '[]',
            cleaned TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
            UNIQUE(line_id, seq));
        CREATE TABLE batch_material(batch_no TEXT NOT NULL, lot_no TEXT NOT NULL,
            PRIMARY KEY(batch_no, lot_no));
        CREATE TABLE material_lot_lock(lot_no TEXT PRIMARY KEY, locked INTEGER NOT NULL,
            reason TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE material_lot_lock_version(id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_no TEXT, version INTEGER, locked INTEGER, action TEXT, reason TEXT, changed_at TEXT);
        CREATE TABLE shipment_eligibility(batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL,
            status TEXT NOT NULL, locked_lots TEXT NOT NULL DEFAULT '[]',
            reasons TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility_version(id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT, version INTEGER, status TEXT, locked_lots TEXT DEFAULT '[]',
            reasons TEXT DEFAULT '[]', trigger TEXT, changed_at TEXT);
        CREATE TABLE adjudication(batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL,
            actual TEXT DEFAULT '[]', undeclared TEXT DEFAULT '[]', disposition TEXT NOT NULL,
            reasons TEXT DEFAULT '[]', sources TEXT DEFAULT '{}', decided_at TEXT NOT NULL);
        CREATE TABLE adjudication_version(id INTEGER PRIMARY KEY AUTOINCREMENT, batch_no TEXT,
            line_id TEXT, seq INTEGER, version INTEGER, actual TEXT DEFAULT '[]',
            undeclared TEXT DEFAULT '[]', disposition TEXT, reasons TEXT DEFAULT '[]',
            sources TEXT DEFAULT '{}', trigger TEXT, decided_at TEXT);
        CREATE TABLE verification(verification_no TEXT PRIMARY KEY, status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT, updated_at TEXT, confirmed_at TEXT);
        CREATE TABLE verification_batch(verification_no TEXT, batch_no TEXT, position INTEGER,
            snapshot_version INTEGER, snapshot_disposition TEXT, snapshot_actual TEXT DEFAULT '[]',
            snapshot_undeclared TEXT DEFAULT '[]', snapshot_reasons TEXT DEFAULT '[]',
            snapshot_ship_version INTEGER, snapshot_ship_status TEXT,
            snapshot_locked_lots TEXT DEFAULT '[]', snapshot_ship_reasons TEXT DEFAULT '[]',
            PRIMARY KEY(verification_no, batch_no));
        CREATE TABLE verification_confirmation(id INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_no TEXT, confirmed_at TEXT);
        CREATE TABLE verification_confirmation_batch(confirmation_id INTEGER, verification_no TEXT,
            position INTEGER, batch_no TEXT, snapshot_version INTEGER, snapshot_disposition TEXT,
            snapshot_actual TEXT DEFAULT '[]', snapshot_undeclared TEXT DEFAULT '[]',
            snapshot_reasons TEXT DEFAULT '[]', snapshot_ship_version INTEGER,
            snapshot_ship_status TEXT, snapshot_locked_lots TEXT DEFAULT '[]',
            snapshot_ship_reasons TEXT DEFAULT '[]');
        CREATE TABLE shipment_handover(handover_no TEXT PRIMARY KEY, verification_no TEXT,
            verification_revision INTEGER, confirmation_id INTEGER, confirmed_at TEXT,
            batch_count INTEGER, handed_over_at TEXT);
        CREATE TABLE shipment_handover_batch(handover_no TEXT, position INTEGER, batch_no TEXT,
            confirmation_id INTEGER, snapshot_version INTEGER, snapshot_disposition TEXT,
            snapshot_actual TEXT DEFAULT '[]', snapshot_undeclared TEXT DEFAULT '[]',
            snapshot_reasons TEXT DEFAULT '[]', snapshot_ship_version INTEGER,
            snapshot_ship_status TEXT, snapshot_locked_lots TEXT DEFAULT '[]',
            snapshot_ship_reasons TEXT DEFAULT '[]', PRIMARY KEY(handover_no, batch_no));
        CREATE TABLE shipment_handover_occupancy(batch_no TEXT PRIMARY KEY, handover_no TEXT,
            position INTEGER, handed_over_at TEXT);
        CREATE TABLE shipment_review(id INTEGER PRIMARY KEY AUTOINCREMENT, handover_no TEXT,
            batch_no TEXT, position INTEGER, seen_adjudication_version INTEGER,
            seen_shipment_version INTEGER, seen_batch_voided INTEGER, disposition TEXT,
            reason TEXT, reviewed_at TEXT);
        CREATE TABLE shipment_return(return_no TEXT PRIMARY KEY, handover_no TEXT, reason TEXT,
            batch_count INTEGER, returned_at TEXT);
        CREATE TABLE shipment_return_batch(return_no TEXT, handover_no TEXT, position INTEGER,
            batch_no TEXT, PRIMARY KEY(return_no, batch_no), UNIQUE(handover_no, batch_no));
        INSERT INTO allergen(code, name_zh) VALUES('milk', '乳');
        INSERT INTO line VALUES('L1', 't');
        INSERT INTO material_lot VALUES('RM-M', '奶粉', '["milk"]', 't');
        INSERT INTO batch VALUES('OLD1', 'L1', 1, '["RM-M"]', '["milk"]', '[]', 't');
        INSERT INTO batch_material VALUES('OLD1', 'RM-M');
        INSERT INTO adjudication VALUES('OLD1', 1, '["milk"]', '[]', 'RELEASE', '[]', '{}', 't');
        INSERT INTO adjudication_version(batch_no, line_id, seq, version, disposition, trigger, decided_at)
            VALUES('OLD1', 'L1', 1, 1, 'RELEASE', 'REGISTER', 't');
        INSERT INTO shipment_eligibility VALUES('OLD1', 1, 'SHIPPABLE', '[]', '[]', 't');
        INSERT INTO shipment_eligibility_version(batch_no, version, status, trigger, changed_at)
            VALUES('OLD1', 1, 'SHIPPABLE', 'REGISTER', 't');
        INSERT INTO verification VALUES('V1', 'CONFIRMED', 2, 't', 't', 't');
        INSERT INTO verification_batch VALUES('V1', 'OLD1', 1, 1, 'RELEASE', '[]', '[]', '[]',
            1, 'SHIPPABLE', '[]', '[]');
        INSERT INTO verification_confirmation VALUES(1, 'V1', 't');
        INSERT INTO verification_confirmation_batch VALUES(1, 'V1', 1, 'OLD1', 1, 'RELEASE',
            '[]', '[]', '[]', 1, 'SHIPPABLE', '[]', '[]');
        INSERT INTO shipment_handover VALUES('H1', 'V1', 2, 1, 't', 1, 't');
        INSERT INTO shipment_handover_batch VALUES('H1', 1, 'OLD1', 1, 1, 'RELEASE',
            '[]', '[]', '[]', 1, 'SHIPPABLE', '[]', '[]');
        INSERT INTO shipment_handover_occupancy VALUES('OLD1', 'H1', 1, 't');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        # 旧批现行号自别名已回填，可查
        assert c.get("/api/batches/OLD1").status_code == 200
        # 历史快照 recorded_batch_no 回填为当时号
        h = c.get("/api/handovers/H1").json()
        assert h["batches"][0]["recorded_batch_no"] == "OLD1"
        assert h["batches"][0]["current_batch_no"] == "OLD1"
        # 更名后旧号成为历史别名，占用不释放
        resp = c.post(
            "/api/batches/OLD1/rename",
            json={"new_batch_no": "NEW1", "reason": "录错"},
        )
        assert resp.status_code == 200, resp.text
        assert c.get("/api/batches/OLD1").json()["batch_no"] == "NEW1"
        assert c.get("/api/batches/NEW1/handover").json()["shipped"] is True
        h = c.get("/api/handovers/H1").json()
        assert h["batches"][0]["recorded_batch_no"] == "OLD1"
        assert h["batches"][0]["current_batch_no"] == "NEW1"

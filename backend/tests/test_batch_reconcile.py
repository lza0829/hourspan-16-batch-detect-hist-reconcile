"""批管道三方对账测试：架构乙（id 集全等）+ 失败闭甲（开段与对账同事务）。

覆盖任务要求的分别核对：
  · 含变更与未变更的一批应对上（且 valid 与入档四字段无关）；
  · 全未变更的一批也应对上；
  · 「changed 未开段」须失败：回滚、批次不得标成功、失败对账须持久可观测；
  · 「计数相等但 id 错位」：甲口径（counts_relation_ok）通过、乙口径（valid）必须失败；
  · 邻批不得串改本批：跨批跑次拒绝、外批快照混入判 invalid、已应用批禁止重应用。
"""

import json
import os
from pathlib import Path

from app.db import connect
from app.modules import batch_detect_hist_reconcile as rec
from app.services import detect, ingest


def write_fixture(name, snapshots):
    path = Path(os.environ["FIXTURES_DIR"]) / name
    path.write_text(
        json.dumps({"snapshots": snapshots}, ensure_ascii=False), encoding="utf-8"
    )
    return name


def snap(sid, store, **kw):
    base = {
        "id": sid,
        "store_id": store,
        "as_of_ts": "2026-11-15T00:00:00Z",
        "open_time": "09:00",
        "close_time": "21:00",
        "weekday_mask": "1111100",
        "biz_status": "open",
    }
    base.update(kw)
    return base


def test_mixed_batch_reconciles_and_is_not_four_field_counting(fresh_db):
    """含变更与未变更的一批应对上；valid 由 id 集判定，与 accepted/skipped 四字段无关。"""
    write_fixture(
        "mixed.json",
        [
            snap("SNAP-T1", "ST-EAST-01", open_time="09:30", close_time="21:30"),  # 变更
            snap("SNAP-T2", "ST-EAST-02", open_time="10:00", close_time="22:00",
                 weekday_mask="1111110"),  # 与种子一致 → 未变更
            snap("SNAP-T3", "ST-GHOST-99"),  # 未知门店 → 入档 skipped
        ],
    )
    ing = ingest.run_batch("mixed.json")
    assert ing["accepted"] == 2 and ing["skipped"] == 1 and ing["total"] == 3
    batch_id = ing["batch_id"]

    det = detect.run_detect(batch_id)
    assert det["changed_count"] == 1
    assert det["changes"][0]["snapshot_id"] == "SNAP-T1"

    res = rec.apply_batch(batch_id, det["detect_run_id"])
    assert res["ok"] and res["valid"]
    assert res["counts"] == {"snapshot": 2, "changed": 1, "opened": 1}
    assert res["changed_ids"] == ["SNAP-T1"] == res["opened_ids"]
    assert res["counts_relation_ok"] and res["id_set_equal"]
    assert res["rule"] == "B_ID_SET" and res["closure"] == "A_SAME_TX"
    # 对账与入档四字段无涉：accepted=2 ≠ changed=1，仍判通过
    assert ing["accepted"] != res["counts"]["changed"]

    conn = connect()
    try:
        batch = conn.execute(
            "SELECT * FROM ingest_batches WHERE id=?", (batch_id,)
        ).fetchone()
        assert batch["apply_status"] == "applied" and batch["applied_at"]
        assert batch["apply_detect_run_id"] == det["detect_run_id"]

        rows = conn.execute(
            "SELECT * FROM batch_reconcile WHERE batch_id=?", (batch_id,)
        ).fetchall()
        assert len(rows) == 1 and rows[0]["valid"] == 1

        # 新开段归属本次对账单号；旧段已关；当前镜已切
        opened = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE opened_by_run=?",
            (rows[0]["id"],),
        ).fetchall()
        assert len(opened) == 1 and opened[0]["source_snapshot_id"] == "SNAP-T1"
        seed_hist = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id='HIST-SEED-ST-EAST-01'"
        ).fetchone()
        assert seed_hist["is_current"] == 0 and seed_hist["valid_to"]
        mirror = conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id='ST-EAST-01'"
        ).fetchone()
        assert mirror["open_time"] == "09:30" and mirror["hist_id"] == opened[0]["id"]
        # 未变更门店不动
        east2 = conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id='ST-EAST-02'"
        ).fetchone()
        assert east2["c"] == 1

        # 三方对账核证（只读）：三段计数、id 集摘要、valid、核证检查全过
        v = rec.verify_batch(conn, batch_id)
        assert v["valid"] is True and v["attestation"]["ok"]
        assert v["counts"] == {"snapshot": 2, "changed": 1, "opened": 1}
        assert v["id_sets"]["changed"] == ["SNAP-T1"] == v["id_sets"]["opened"]
        assert v["live"]["opened_ids"] == ["SNAP-T1"]
    finally:
        conn.close()


def test_zero_change_batch_reconciles(fresh_db):
    """全未变更的一批：空 changed 集 == 空 opened 集，应对上。"""
    write_fixture("nochange.json", [snap("SNAP-U1", "ST-EAST-01")])  # 与种子完全一致
    ing = ingest.run_batch("nochange.json")
    det = detect.run_detect(ing["batch_id"])
    assert det["changed_count"] == 0

    res = rec.apply_batch(ing["batch_id"], det["detect_run_id"])
    assert res["ok"] and res["valid"]
    assert res["counts"] == {"snapshot": 1, "changed": 0, "opened": 0}

    conn = connect()
    try:
        hist_n = conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE store_id='ST-EAST-01'"
        ).fetchone()["c"]
        assert hist_n == 1  # 无新开段
        assert conn.execute(
            "SELECT apply_status FROM ingest_batches WHERE id=?", (ing["batch_id"],)
        ).fetchone()[0] == "applied"
    finally:
        conn.close()


def test_changed_without_open_fails_and_rolls_back(fresh_db):
    """changed 未开段须失败：整体回滚、批次标 failed、失败对账持久可观测。"""
    write_fixture("missing.json", [snap("SNAP-M1", "ST-EAST-01", open_time="08:00")])
    ing = ingest.run_batch("missing.json")
    det = detect.run_detect(ing["batch_id"])
    assert det["changed_count"] == 1

    conn = connect()
    hist_before = conn.execute(
        "SELECT COUNT(*) AS c FROM dim_store_hours_hist"
    ).fetchone()["c"]
    mirror_before = dict(
        conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id='ST-EAST-01'"
        ).fetchone()
    )
    # 开段前快照丢失 → 检测多报未开段
    conn.execute("DELETE FROM source_snapshots WHERE id='SNAP-M1'")
    conn.commit()
    conn.close()

    res = rec.apply_batch(ing["batch_id"], det["detect_run_id"])
    assert not res["ok"] and not res["valid"]
    assert res["counts"]["changed"] == 1 and res["counts"]["opened"] == 0
    assert res["missing_opened"] == ["SNAP-M1"]
    assert res["apply_status"] == "failed"

    conn = connect()
    try:
        batch = conn.execute(
            "SELECT * FROM ingest_batches WHERE id=?", (ing["batch_id"],)
        ).fetchone()
        assert batch["apply_status"] == "failed"  # 不一致不得标该批应用成功
        # 对账失败仍持久化（禁止对账未记），且可观测
        rows = conn.execute(
            "SELECT * FROM batch_reconcile WHERE batch_id=?", (ing["batch_id"],)
        ).fetchall()
        assert len(rows) == 1 and rows[0]["valid"] == 0
        assert json.loads(rows[0]["detail_json"])["rolled_back"] is True
        # 禁止对账失败但正式 hist/镜已切换
        assert (
            conn.execute("SELECT COUNT(*) AS c FROM dim_store_hours_hist").fetchone()["c"]
            == hist_before
        )
        mirror_after = conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id='ST-EAST-01'"
        ).fetchone()
        assert mirror_after["row_hash"] == mirror_before["row_hash"]
        assert mirror_after["hist_id"] == mirror_before["hist_id"]
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE opened_by_run=?",
            (rows[0]["id"],),
        ).fetchone()["c"] == 0

        v = rec.verify_batch(conn, ing["batch_id"])
        assert v["valid"] is False
        checks = {c["check"]: c["ok"] for c in v["attestation"]["checks"]}
        assert checks["rolled_back_no_opened"] and checks["batch_marked_failed"]
        assert v["attestation"]["ok"]
    finally:
        conn.close()


def test_counts_equal_but_ids_misaligned_fails_under_id_set_rule(fresh_db):
    """计数相等但 id 错位：甲口径（计数）会通过、乙口径（id 集）必须失败。"""
    # 纯规则：计数 1==1 碰巧相等，id 集 {A}≠{B}
    verdict = rec.evaluate(["SNAP-A"], ["SNAP-B"], ["SNAP-A", "SNAP-B"])
    assert verdict["counts"]["changed"] == verdict["counts"]["opened"] == 1
    assert verdict["counts_relation_ok"] is True  # 甲口径展示位：会通过
    assert verdict["id_set_equal"] is False
    assert verdict["valid"] is False  # 乙口径：必须失败

    # 邻批快照混入：id 集自洽但含外来成员，同样失败
    foreign = rec.evaluate(["SNAP-X"], ["SNAP-X"], ["SNAP-A"])
    assert foreign["id_set_equal"] is True
    assert foreign["foreign_changed"] == ["SNAP-X"]
    assert foreign["valid"] is False

    # 集成：成功应用后篡改新开段来源 id（计数仍相等、id 错位）→ 核证接口必须暴露
    write_fixture("tamper.json", [snap("SNAP-A", "ST-EAST-01", open_time="08:30")])
    ing = ingest.run_batch("tamper.json")
    det = detect.run_detect(ing["batch_id"])
    res = rec.apply_batch(ing["batch_id"], det["detect_run_id"])
    assert res["valid"]

    conn = connect()
    try:
        conn.execute(
            "UPDATE dim_store_hours_hist SET source_snapshot_id='SNAP-B' WHERE opened_by_run=?",
            (res["reconcile_id"],),
        )
        conn.commit()
        v = rec.verify_batch(conn, ing["batch_id"])
        assert v["attestation"]["ok"] is False
        assert v["live"]["opened_count"] == 1  # 计数仍 1==1
        assert v["live"]["opened_ids"] == ["SNAP-B"]  # id 已错位
        assert v["live"]["id_set_equal"] is False
        checks = {c["check"]: c["ok"] for c in v["attestation"]["checks"]}
        assert checks["live_opened_eq_record"] is False
    finally:
        conn.close()


def test_neighbor_batch_cannot_tamper(fresh_db):
    """邻批不得串改本批：跨批跑次拒绝、外批快照混入判失败、已应用禁止重应用。"""
    write_fixture("b1.json", [snap("SNAP-B1", "ST-EAST-01", open_time="08:00")])
    write_fixture("b2.json", [snap("SNAP-B2", "ST-EAST-02", open_time="08:00")])
    b1 = ingest.run_batch("b1.json")["batch_id"]
    b2 = ingest.run_batch("b2.json")["batch_id"]
    run1 = detect.run_detect(b1)["detect_run_id"]
    run2 = detect.run_detect(b2)["detect_run_id"]

    # 邻批跑次不得用于本批
    res = rec.apply_batch(b1, run2)
    assert res["ok"] is False and res["error"] == "cross_batch_run"
    conn = connect()
    try:
        assert conn.execute(
            "SELECT apply_status FROM ingest_batches WHERE id=?", (b1,)
        ).fetchone()[0] == "pending"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM batch_reconcile WHERE batch_id=?", (b1,)
        ).fetchone()["c"] == 0
    finally:
        conn.close()

    # 本批检测报告被串入邻批快照 → 外来成员 → 对账失败且整体回滚
    conn = connect()
    report = json.loads(
        conn.execute("SELECT report_json FROM detect_runs WHERE id=?", (run1,)).fetchone()[0]
    )
    report["changes"][0]["snapshot_id"] = "SNAP-B2"
    conn.execute(
        "UPDATE detect_runs SET report_json=? WHERE id=?", (json.dumps(report), run1)
    )
    conn.commit()
    conn.close()

    res = rec.apply_batch(b1, run1)
    assert res["ok"] is False and res["valid"] is False
    assert res["foreign_changed"] == ["SNAP-B2"]
    conn = connect()
    try:
        assert conn.execute(
            "SELECT apply_status FROM ingest_batches WHERE id=?", (b1,)
        ).fetchone()[0] == "failed"
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE opened_by_run=?",
            (res["reconcile_id"],),
        ).fetchone()["c"] == 0
        # 邻批门店镜像未被串改
        east2 = conn.execute(
            "SELECT * FROM dim_store_hours_current WHERE store_id='ST-EAST-02'"
        ).fetchone()
        assert east2["open_time"] == "10:00"
    finally:
        conn.close()

    # 邻批正常应用本批 → 成功；重复应用被拒
    ok = rec.apply_batch(b2, run2)
    assert ok["ok"] and ok["valid"]
    again = rec.apply_batch(b2, run2)
    assert again["ok"] is False and again["error"] == "already_applied"


def test_unbatched_detect_run_refused(fresh_db):
    """全局检测跑次（无批归属）禁止按批应用。"""
    write_fixture("b9.json", [snap("SNAP-B9", "ST-EAST-01", open_time="07:30")])
    batch_id = ingest.run_batch("b9.json")["batch_id"]
    global_run = detect.run_detect(None)["detect_run_id"]  # 跨批最新快照跑次
    res = rec.apply_batch(batch_id, global_run)
    assert res["ok"] is False and res["error"] == "run_unbatched"

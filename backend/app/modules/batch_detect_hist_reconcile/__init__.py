"""批管道三方对账 · batch_detect_hist_reconcile

同一入档批：检测 changed 成员 与 Type2 新开 hist 段 必须逐批持久对账，
不一致则该批不得标应用成功。这是批管道对账，不是入档 accepted/skipped 四字段计数壳。

全局唯一架构选定（两种可行架构只贯彻其一）：

  对账架构 = 乙（id 集全等为准）
    valid ⟺ 检测 changed 的 snapshot_id 集合 == 成功新开 hist 的 source_snapshot_id 集合
            且 changed 无跨批外来成员（邻批快照不得混入本批）。
    三段计数（快照实落数 / changed 成员数 / 成功新开 hist 数）仅作展示；
    计数碰巧相等但 id 错位必须失败 —— counts_relation_ok 是甲口径展示位，不参与 valid 判定。

  失败闭 = 甲（开段与对账同事务）
    关旧段 + 开新段 + 当前镜切换 + 对账记录落库 + 批次应用态，同一事务原子提交；
    对账 invalid → 整体回滚（正式 hist/镜不切换），另起短事务持久化失败对账记录并把批次
    标为 failed —— 失败必须可观测，且绝不出现「对账失败但正式 hist/镜已切换」。

禁止项落实：
  · 不以入档 accepted/skipped/total 四字段冒充对账：valid 只由 id 集判定，不读入档报告计数。
  · hist 已新开但对账未记：成功路径上开段与对账记录同事务提交，不存在无对账的新开段。
  · 对账失败但正式 hist/镜已切换：失败路径整体回滚；核证接口复查 live.opened==0。
  · 邻批不得串改本批：detect_run 必须归属本批；changed 含外批快照即 invalid；
    新开段以 opened_by_run=对账单号归属本次应用，邻批开段不计入本批三段计数。
"""

import json
from uuid import uuid4

from app.db import connect
from app.seed import now_iso

RULE = "B_ID_SET"  # 对账架构乙：id 集全等为准，计数仅展示
CLOSURE = "A_SAME_TX"  # 失败闭甲：开段与对账同事务

PENDING = "pending"
APPLIED = "applied"
FAILED = "failed"


def evaluate(changed_ids, opened_ids, batch_snapshot_ids):
    """纯规则判定：给定三方 id 集，产出三段计数、差集摘要与 valid（乙口径）。

    changed_ids:        检测 changed 成员的 snapshot_id
    opened_ids:         成功新开 hist 段的 source_snapshot_id
    batch_snapshot_ids: 本批实落快照 id（快照实落数的集合基）
    """
    changed = sorted({i for i in changed_ids if i})
    opened = sorted({i for i in opened_ids if i})
    batch_snaps = sorted({i for i in batch_snapshot_ids if i})
    missing_opened = sorted(set(changed) - set(opened))  # 检测多报未开段
    extra_opened = sorted(set(opened) - set(changed))  # hist 新开了检测无成员
    foreign_changed = sorted(set(changed) - set(batch_snaps))  # 邻批快照混入本批
    # 甲口径（仅展示）：三段计数关系满足约定 新开==changed<=实落
    counts_relation_ok = len(opened) == len(changed) <= len(batch_snaps)
    # 乙口径（valid 判据）：id 集全等且无跨批外来成员
    id_set_equal = changed == opened
    valid = id_set_equal and not foreign_changed
    return {
        "counts": {
            "snapshot": len(batch_snaps),
            "changed": len(changed),
            "opened": len(opened),
        },
        "changed_ids": changed,
        "opened_ids": opened,
        "missing_opened": missing_opened,
        "extra_opened": extra_opened,
        "foreign_changed": foreign_changed,
        "counts_relation_ok": counts_relation_ok,
        "id_set_equal": id_set_equal,
        "valid": valid,
    }


def _insert_record(conn, recon_id, batch_id, detect_run_id, verdict, now, detail):
    conn.execute(
        """INSERT INTO batch_reconcile(
          id, batch_id, detect_run_id,
          snapshot_count, changed_count, opened_count,
          counts_relation_ok, id_set_equal, valid,
          changed_ids_json, opened_ids_json,
          missing_opened_json, extra_opened_json, foreign_changed_json,
          rule, closure, detail_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            recon_id,
            batch_id,
            detect_run_id,
            verdict["counts"]["snapshot"],
            verdict["counts"]["changed"],
            verdict["counts"]["opened"],
            int(verdict["counts_relation_ok"]),
            int(verdict["id_set_equal"]),
            int(verdict["valid"]),
            json.dumps(verdict["changed_ids"], ensure_ascii=False),
            json.dumps(verdict["opened_ids"], ensure_ascii=False),
            json.dumps(verdict["missing_opened"], ensure_ascii=False),
            json.dumps(verdict["extra_opened"], ensure_ascii=False),
            json.dumps(verdict["foreign_changed"], ensure_ascii=False),
            RULE,
            CLOSURE,
            json.dumps(detail, ensure_ascii=False),
            now,
        ),
    )


def apply_batch(batch_id, detect_run_id):
    """对同一入档批应用 Type2 开闭并持久三方对账；不一致则整体回滚且批次不得标成功。"""
    conn = connect()
    now = now_iso()
    try:
        batch = conn.execute(
            "SELECT * FROM ingest_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            return {"ok": False, "error": "batch_not_found", "message": f"入档批不存在: {batch_id}"}
        run = conn.execute(
            "SELECT * FROM detect_runs WHERE id=?", (detect_run_id,)
        ).fetchone()
        if not run:
            return {"ok": False, "error": "detect_run_not_found", "message": f"检测跑次不存在: {detect_run_id}"}
        if not run["batch_id"]:
            return {"ok": False, "error": "run_unbatched", "message": "全局检测跑次无批归属，禁止按批应用"}
        if run["batch_id"] != batch_id:
            return {
                "ok": False,
                "error": "cross_batch_run",
                "message": f"检测跑次属批 {run['batch_id']}，不得用于批 {batch_id}（邻批不得串改本批）",
            }
        if batch["apply_status"] == APPLIED:
            return {"ok": False, "error": "already_applied", "message": f"批 {batch_id} 已应用成功，禁止重复应用"}

        report = json.loads(run["report_json"])
        changes = report.get("changes", [])
        changed_ids = [ch.get("snapshot_id") for ch in changes if ch.get("snapshot_id")]
        batch_snap_ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM source_snapshots WHERE batch_id=?", (batch_id,)
            )
        ]

        recon_id = f"REC-{uuid4().hex[:8]}"
        applied = []
        wiring_mismatch = []
        # ---- 失败闭甲：开段与对账同事务 ----
        try:
            for ch in changes:
                snap_id = ch.get("snapshot_id")
                snap = (
                    conn.execute(
                        "SELECT * FROM source_snapshots WHERE id=?", (snap_id,)
                    ).fetchone()
                    if snap_id
                    else None
                )
                if not snap:
                    continue  # 检测多报未开段：opened 缺员 → 对账 invalid → 回滚
                if snap["store_id"] != ch.get("store_id"):
                    wiring_mismatch.append(snap_id)
                    continue  # 快照与门店错线（篡改迹象）：不开段 → 对账 invalid
                store_id = ch["store_id"]
                cur_hist = conn.execute(
                    "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
                    (store_id,),
                ).fetchone()
                valid_from = snap["as_of_ts"]
                if cur_hist:
                    conn.execute(
                        """UPDATE dim_store_hours_hist
                           SET valid_to=?, is_current=0, closed_by_run=?
                           WHERE id=?""",
                        (valid_from, detect_run_id, cur_hist["id"]),
                    )
                new_id = f"HIST-{uuid4().hex[:10]}"
                conn.execute(
                    """INSERT INTO dim_store_hours_hist(
                      id, store_id, open_time, close_time, weekday_mask, biz_status,
                      valid_from, valid_to, is_current, source_snapshot_id, row_hash,
                      closed_by_run, opened_by_run
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id,
                        store_id,
                        snap["open_time"],
                        snap["close_time"],
                        snap["weekday_mask"],
                        snap["biz_status"],
                        valid_from,
                        None,
                        1,
                        snap["id"],
                        snap["row_hash"],
                        None,
                        recon_id,
                    ),
                )
                conn.execute(
                    """INSERT INTO dim_store_hours_current(
                      store_id, open_time, close_time, weekday_mask, biz_status,
                      hist_id, as_of_ts, row_hash
                    ) VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(store_id) DO UPDATE SET
                      open_time=excluded.open_time,
                      close_time=excluded.close_time,
                      weekday_mask=excluded.weekday_mask,
                      biz_status=excluded.biz_status,
                      hist_id=excluded.hist_id,
                      as_of_ts=excluded.as_of_ts,
                      row_hash=excluded.row_hash
                    """,
                    (
                        store_id,
                        snap["open_time"],
                        snap["close_time"],
                        snap["weekday_mask"],
                        snap["biz_status"],
                        new_id,
                        valid_from,
                        snap["row_hash"],
                    ),
                )
                applied.append(
                    {
                        "store_id": store_id,
                        "hist_id": new_id,
                        "snapshot_id": snap["id"],
                        "closed": cur_hist["id"] if cur_hist else None,
                    }
                )
            opened_ids = [
                r["source_snapshot_id"]
                for r in conn.execute(
                    "SELECT DISTINCT source_snapshot_id FROM dim_store_hours_hist WHERE opened_by_run=?",
                    (recon_id,),
                )
            ]
            verdict = evaluate(changed_ids, opened_ids, batch_snap_ids)
            if verdict["valid"]:
                # 开段 + 对账记录 + 批次应用态：同一事务原子提交
                _insert_record(
                    conn,
                    recon_id,
                    batch_id,
                    detect_run_id,
                    verdict,
                    now,
                    {"rolled_back": False, "store_wiring_mismatch": wiring_mismatch},
                )
                conn.execute(
                    """UPDATE ingest_batches
                       SET apply_status=?, applied_at=?, apply_detect_run_id=?
                       WHERE id=?""",
                    (APPLIED, now, detect_run_id, batch_id),
                )
                conn.commit()
                return {
                    "ok": True,
                    "error": None,
                    "batch_id": batch_id,
                    "detect_run_id": detect_run_id,
                    "reconcile_id": recon_id,
                    "apply_status": APPLIED,
                    "rule": RULE,
                    "closure": CLOSURE,
                    "applied": applied,
                    **verdict,
                    "message": "三方对账通过（乙：id 集全等），批次已标应用成功",
                }
            conn.rollback()  # 对账不一致：开段/关段/镜切换整体撤销
        except Exception:
            conn.rollback()
            raise
        # ---- 失败落库（另起短事务）：对账失败必须持久可观测，批次标 failed ----
        _insert_record(
            conn,
            recon_id,
            batch_id,
            detect_run_id,
            verdict,
            now,
            {"rolled_back": True, "store_wiring_mismatch": wiring_mismatch},
        )
        conn.execute(
            "UPDATE ingest_batches SET apply_status=?, apply_detect_run_id=? WHERE id=?",
            (FAILED, detect_run_id, batch_id),
        )
        conn.commit()
        return {
            "ok": False,
            "error": None,
            "batch_id": batch_id,
            "detect_run_id": detect_run_id,
            "reconcile_id": recon_id,
            "apply_status": FAILED,
            "rule": RULE,
            "closure": CLOSURE,
            "applied": [],
            **verdict,
            "message": "三方对账不一致：开段已整体回滚，批次未标应用成功",
        }
    finally:
        conn.close()


def _row_to_record(row):
    rec = dict(row)
    for key in (
        "changed_ids_json",
        "opened_ids_json",
        "missing_opened_json",
        "extra_opened_json",
        "foreign_changed_json",
    ):
        rec[key[: -len("_json")]] = json.loads(rec.pop(key))
    rec["counts_relation_ok"] = bool(rec["counts_relation_ok"])
    rec["id_set_equal"] = bool(rec["id_set_equal"])
    rec["valid"] = bool(rec["valid"])
    rec["detail"] = json.loads(rec.pop("detail_json"))
    return rec


def list_records(conn, limit=50):
    rows = conn.execute(
        "SELECT * FROM batch_reconcile ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_record(r) for r in rows]


def verify_batch(conn, batch_id):
    """三方对账核证（只读）：重算三段计数与 id 集，比对持久对账记录，给出 valid 与核证检查。

    成功单：live 重算须与记录一致（事后/邻批串改会在此暴露），批次须为 applied；
    失败单：live 须无本次新开段（回滚核实），批次须为 failed。
    """
    batch = conn.execute(
        "SELECT * FROM ingest_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if not batch:
        return None
    batch_snap_ids = sorted(
        r["id"]
        for r in conn.execute(
            "SELECT id FROM source_snapshots WHERE batch_id=?", (batch_id,)
        )
    )
    records = conn.execute(
        "SELECT * FROM batch_reconcile WHERE batch_id=? ORDER BY created_at DESC, rowid DESC",
        (batch_id,),
    ).fetchall()
    latest = _row_to_record(records[0]) if records else None

    live = {"snapshot_count": len(batch_snap_ids), "snapshot_ids": batch_snap_ids}
    checks = [{"check": "reconcile_record_exists", "ok": latest is not None}]
    if latest:
        run = conn.execute(
            "SELECT * FROM detect_runs WHERE id=?", (latest["detect_run_id"],)
        ).fetchone()
        changed_ids = []
        if run:
            report = json.loads(run["report_json"])
            changed_ids = sorted(
                {
                    ch.get("snapshot_id")
                    for ch in report.get("changes", [])
                    if ch.get("snapshot_id")
                }
            )
        opened_ids = sorted(
            r["source_snapshot_id"]
            for r in conn.execute(
                "SELECT DISTINCT source_snapshot_id FROM dim_store_hours_hist WHERE opened_by_run=?",
                (latest["id"],),
            )
        )
        live_verdict = evaluate(changed_ids, opened_ids, batch_snap_ids)
        live.update(
            {
                "changed_count": live_verdict["counts"]["changed"],
                "opened_count": live_verdict["counts"]["opened"],
                "changed_ids": live_verdict["changed_ids"],
                "opened_ids": live_verdict["opened_ids"],
                "id_set_equal": live_verdict["id_set_equal"],
                "valid": live_verdict["valid"],
            }
        )
        checks.append(
            {
                "check": "live_changed_eq_record",
                "ok": live_verdict["changed_ids"] == latest["changed_ids"],
            }
        )
        if latest["valid"]:
            checks.append(
                {
                    "check": "live_opened_eq_record",
                    "ok": live_verdict["opened_ids"] == latest["opened_ids"],
                }
            )
            checks.append({"check": "live_valid", "ok": live_verdict["valid"]})
            checks.append(
                {"check": "batch_marked_applied", "ok": batch["apply_status"] == APPLIED}
            )
        else:
            checks.append({"check": "rolled_back_no_opened", "ok": not opened_ids})
            checks.append(
                {"check": "batch_marked_failed", "ok": batch["apply_status"] == FAILED}
            )
    attestation_ok = all(c["ok"] for c in checks)
    return {
        "batch_id": batch_id,
        "apply_status": batch["apply_status"],
        "applied_at": batch["applied_at"],
        "rule": RULE,
        "closure": CLOSURE,
        "valid": latest["valid"] if latest else None,
        "counts": (
            {
                "snapshot": latest["snapshot_count"],
                "changed": latest["changed_count"],
                "opened": latest["opened_count"],
            }
            if latest
            else {"snapshot": len(batch_snap_ids), "changed": None, "opened": None}
        ),
        "id_sets": (
            {
                "changed": latest["changed_ids"],
                "opened": latest["opened_ids"],
                "missing_opened": latest["missing_opened"],
                "extra_opened": latest["extra_opened"],
                "foreign_changed": latest["foreign_changed"],
            }
            if latest
            else None
        ),
        "counts_relation_ok": latest["counts_relation_ok"] if latest else None,
        "id_set_equal": latest["id_set_equal"] if latest else None,
        "record": latest,
        "records_total": len(records),
        "live": live,
        "attestation": {"ok": attestation_ok, "checks": checks},
    }

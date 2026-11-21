from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import STUBS
from app.modules import batch_detect_hist_reconcile as reconcile
from app.services import catalog, detect, ingest, scd

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = catalog.overview(conn)
        data["空桩"] = STUBS
        return data
    finally:
        conn.close()


@router.get("/stores")
def stores():
    conn = connect()
    try:
        return catalog.store_board(conn)
    finally:
        conn.close()


@router.get("/snapshots")
def snapshots(batch_id: str | None = None):
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM source_snapshots WHERE batch_id=? ORDER BY as_of_ts, store_id",
                (batch_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_snapshots ORDER BY as_of_ts DESC, store_id LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/batches")
def batches():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_batches ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/history")
def history(store_id: str | None = None):
    conn = connect()
    try:
        if store_id:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? ORDER BY valid_from, id",
                (store_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dim_store_hours_hist ORDER BY store_id, valid_from, id"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/current")
def current_mirror():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM dim_store_hours_current ORDER BY store_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/detect-runs")
def detect_runs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM detect_runs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/proofs")
def proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scd_proofs ORDER BY id DESC LIMIT 80"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest(body: dict | None = Body(None)):
    name = (body or {}).get("fixture", "ingest_change_batch.json")
    return ingest.run_batch(name)


@router.post("/detect/run")
def run_detect(body: dict | None = Body(None)):
    batch_id = (body or {}).get("batch_id")
    return detect.run_detect(batch_id)


@router.post("/scd/apply")
def run_scd(body: dict = Body(...)):
    batch_id = body.get("batch_id")
    run_id = body.get("detect_run_id")
    if not batch_id or not run_id:
        raise HTTPException(400, "需要 batch_id 与 detect_run_id（按批对账应用）")
    result = reconcile.apply_batch(batch_id, run_id)
    if result.get("error"):
        status = {
            "batch_not_found": 404,
            "detect_run_not_found": 404,
        }.get(result["error"], 409)
        raise HTTPException(status, result.get("message", result["error"]))
    return result


@router.get("/reconcile")
def reconcile_list():
    conn = connect()
    try:
        return reconcile.list_records(conn)
    finally:
        conn.close()


@router.get("/reconcile/{batch_id}")
def reconcile_verify(batch_id: str):
    conn = connect()
    try:
        data = reconcile.verify_batch(conn, batch_id)
        if data is None:
            raise HTTPException(404, f"入档批不存在: {batch_id}")
        return data
    finally:
        conn.close()


@router.post("/proofs/rebuild")
def rebuild_proofs():
    return scd.rebuild_proofs()


@router.get("/settings")
def settings():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


@router.get("/stubs")
def stubs():
    return {"stubs": STUBS}

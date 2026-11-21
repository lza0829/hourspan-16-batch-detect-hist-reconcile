"""路由层接线测试：/api/scd/apply 与 /api/reconcile 的守卫与只读核证。"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import detect, ingest  # noqa: E402


def test_apply_requires_batch_and_run(fresh_db):
    client = TestClient(app)
    r = client.post("/api/scd/apply", json={"detect_run_id": "DET-x"})
    assert r.status_code == 400


def test_apply_guard_errors(fresh_db):
    client = TestClient(app)
    assert client.post(
        "/api/scd/apply", json={"batch_id": "NOPE", "detect_run_id": "DET-x"}
    ).status_code == 404
    ing = ingest.run_batch("ingest_change_batch.json")
    assert client.post(
        "/api/scd/apply", json={"batch_id": ing["batch_id"], "detect_run_id": "DET-x"}
    ).status_code == 404


def test_verify_endpoint_roundtrip(fresh_db):
    client = TestClient(app)
    assert client.get("/api/reconcile/NOPE").status_code == 404

    ing = ingest.run_batch("ingest_change_batch.json")
    batch_id = ing["batch_id"]
    det = detect.run_detect(batch_id)
    r = client.post(
        "/api/scd/apply",
        json={"batch_id": batch_id, "detect_run_id": det["detect_run_id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["valid"]
    assert body["counts"]["changed"] == body["counts"]["opened"] == 2

    v = client.get(f"/api/reconcile/{batch_id}")
    assert v.status_code == 200
    data = v.json()
    assert data["valid"] is True and data["attestation"]["ok"]
    assert data["counts"] == {"snapshot": 2, "changed": 2, "opened": 2}
    assert data["id_sets"]["changed"] == data["id_sets"]["opened"]
    assert data["rule"] == "B_ID_SET"

    lst = client.get("/api/reconcile")
    assert lst.status_code == 200
    assert any(row["batch_id"] == batch_id for row in lst.json())

    # 重复应用 → 409
    again = client.post(
        "/api/scd/apply",
        json={"batch_id": batch_id, "detect_run_id": det["detect_run_id"]},
    )
    assert again.status_code == 409

"""Input validation, cross-request durability and write serialization."""
import threading
from datetime import datetime, timezone

from conftest import handover, make_waybill, withdrawal


def test_naive_timestamp_rejected(client):
    make_waybill(client)
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00", "ORIG", "B")]},
    )
    assert r.status_code == 422


def test_revision_must_be_positive(client):
    make_waybill(client)
    bad = handover("S1", 0, "2026-11-18T08:00:00Z", "ORIG", "B")
    r = client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [bad]})
    assert r.status_code == 422


def test_handover_requires_all_fields(client):
    make_waybill(client)
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [{"scan_id": "S1", "revision": 1, "giver": "ORIG"}]},
    )
    assert r.status_code == 422


def test_withdrawal_carries_only_id_and_revision(client):
    make_waybill(client)
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                {
                    "scan_id": "S1",
                    "revision": 2,
                    "event_type": "WITHDRAWAL",
                    "giver": "ORIG",
                }
            ]
        },
    )
    assert r.status_code == 422


def test_batch_internal_same_revision_conflict_rejected(client):
    make_waybill(client)
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "OTHER"),
            ]
        },
    )
    assert r.status_code == 422
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["verified_prefix"] == []


def test_empty_batch_rejected(client):
    make_waybill(client)
    r = client.post("/api/waybills/AWB-1/scans/batch", json={"scans": []})
    assert r.status_code == 422


def test_unknown_event_type_rejected(client):
    make_waybill(client)
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                {"scan_id": "S1", "revision": 1, "event_type": "DELETE"}
            ]
        },
    )
    assert r.status_code == 422


def test_data_survives_restart(client, monkeypatch, tmp_path):
    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )

    # Simulate a process restart: brand-new app + connections against same file.
    import importlib

    from app import config, database, main

    db_path = config.DB_PATH
    importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as new_client:
        chain = new_client.get("/api/waybills/AWB-1/chain").json()
        assert chain["current_custodian"] == "B"
        assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1"]
    assert db_path.exists()


def test_concurrent_writes_serialize_without_mixed_revisions(client):
    """Real threads + real SQLite connections: BEGIN IMMEDIATE serializes writers."""
    import threading

    from app import config, database, schemas, services

    make_waybill(client, origin="ORIG")
    db_path = config.DB_PATH

    errors: list = []
    accepted: list[int] = []
    rejected_stale: list[int] = []
    lock = threading.Lock()

    def worker(rev: int, receiver: str) -> None:
        conn = database.connect(db_path)
        try:
            batch = schemas.BatchSubmit(
                scans=[
                    schemas.ScanEntry(
                        scan_id="S1",
                        revision=rev,
                        ts_utc=datetime(2026, 11, 18, 8, rev, tzinfo=timezone.utc),
                        giver="ORIG",
                        receiver=receiver,
                    )
                ]
            )
            services.apply_batch(conn, "AWB-1", batch)
            with lock:
                accepted.append(rev)
        except services.DomainConflict as exc:
            # A lower revision losing the race against an already-committed
            # higher one is exactly the required serialization behavior.
            assert "stale" in str(exc)
            with lock:
                rejected_stale.append(rev)
        except Exception as exc:  # pragma: no cover - failure signal
            with lock:
                errors.append(str(exc))
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(1, "B")),
        threading.Thread(target=worker, args=(2, "C")),
        threading.Thread(target=worker, args=(3, "D")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    # Every revision was decided exactly once; 3 (highest) must always win.
    assert sorted(accepted + rejected_stale) == [1, 2, 3]
    assert 3 in accepted

    chain = client.get("/api/waybills/AWB-1/chain").json()
    # only the highest revision is adopted, and the chain stays internally consistent
    assert len(chain["verified_prefix"]) == 1
    assert chain["verified_prefix"][0]["revision"] == 3
    assert chain["current_custodian"] == "D"


def test_failed_batch_leaves_old_chain_under_concurrency(client):
    """A rolled-back conflicting batch is never visible to interleaved readers."""
    import threading

    from app import config, database, schemas, services

    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    db_path = config.DB_PATH

    seen: list = []
    stop = threading.Event()

    def reader() -> None:
        conn = database.connect(db_path)
        try:
            while not stop.is_set():
                chain = services.compute_chain(conn, "AWB-1")
                seen.append(
                    (
                        tuple(s.scan_id for s in chain.verified_prefix),
                        chain.current_custodian,
                    )
                )
        finally:
            conn.close()

    t = threading.Thread(target=reader)
    t.start()

    writer = database.connect(db_path)
    try:
        bad = schemas.BatchSubmit(
            scans=[
                schemas.ScanEntry(
                    scan_id="S1",
                    revision=1,  # same revision, divergent content
                    ts_utc=datetime(2026, 11, 18, 8, tzinfo=timezone.utc),
                    giver="ORIG",
                    receiver="TAMPERED",
                )
            ]
        )
        raised = False
        try:
            services.apply_batch(writer, "AWB-1", bad)
        except services.DomainConflict:
            raised = True
    finally:
        writer.close()
        stop.set()
        t.join(timeout=30)

    assert raised
    assert seen, "reader never observed a snapshot"
    assert all(state == (("S1",), "B") for state in seen), seen


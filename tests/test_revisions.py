"""Revision arbitration: highest revision wins, same-revision divergence, stale writes."""
from conftest import handover, make_waybill, withdrawal


def test_highest_revision_wins_and_correction_moves_break(client):
    make_waybill(client)
    # r1: wrong giver -> GAP at S1
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "WRONG", "B")]},
    )
    assert r.status_code == 200
    chain = r.json()["chain"]
    assert chain["break_reason"] == "GAP"
    assert chain["current_custodian"] == "ORIG"
    assert [s["scan_id"] for s in chain["unverified_scans"]] == ["S1"]

    # r2 correction fixes the chain -> break disappears, prefix extends
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 2, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    assert r.status_code == 200
    chain = r.json()["chain"]
    assert chain["chain_intact"] is True
    assert chain["break_reason"] == "NONE"
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1"]
    assert chain["current_custodian"] == "B"
    # adopted revision visible in query output
    assert chain["verified_prefix"][0]["revision"] == 2


def test_same_revision_different_text_whole_batch_rejected(client):
    make_waybill(client)
    ok = handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")
    r = client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [ok]})
    assert r.status_code == 200

    # divergent content at the same revision -> 409
    bad = handover("S1", 1, "2026-11-18T09:00:00Z", "ORIG", "OTHER")
    r = client.post("/api/waybills/AWB-1/scans/batch", json={"scans": [bad]})
    assert r.status_code == 409
    assert "divergence" in r.json()["detail"]

    # old chain preserved
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["verified_prefix"][0]["receiver"] == "B"


def test_conflicting_revision_in_batch_with_innocent_entry_rolls_back_all(client):
    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    # S2 is valid but S1 rev1 conflicts -> whole batch rejected, S2 must not exist
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
                handover("S1", 1, "2026-11-18T08:30:00Z", "ORIG", "X"),
            ]
        },
    )
    assert r.status_code == 409
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1"]


def test_identical_resubmit_is_idempotent(client):
    make_waybill(client)
    payload = {"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]}
    client.post("/api/waybills/AWB-1/scans/batch", json=payload)
    r = client.post("/api/waybills/AWB-1/scans/batch", json=payload)
    assert r.status_code == 200
    assert r.json()["idempotent_duplicates"] == 1
    assert r.json()["applied"] == 0


def test_stale_revision_rejected(client):
    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 3, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 2, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    assert r.status_code == 409
    assert "stale" in r.json()["detail"]


def test_withdrawal_requires_higher_revision_and_removes_scan(client):
    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
            ]
        },
    )
    # lower/equal revision withdrawal -> stale conflict
    r = client.post(
        "/api/waybills/AWB-1/scans/batch", json={"scans": [withdrawal("S2", 1)]}
    )
    assert r.status_code == 409

    r = client.post(
        "/api/waybills/AWB-1/scans/batch", json={"scans": [withdrawal("S2", 2)]}
    )
    assert r.status_code == 200
    chain = r.json()["chain"]
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1"]
    assert chain["current_custodian"] == "B"


def test_withdraw_unknown_scan_rejected(client):
    make_waybill(client)
    r = client.post(
        "/api/waybills/AWB-1/scans/batch", json={"scans": [withdrawal("GHOST", 1)]}
    )
    assert r.status_code == 409
    assert "unknown" in r.json()["detail"]


def test_withdrawal_then_backfill_restores_chain(client):
    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
                handover("S3", 1, "2026-11-18T10:00:00Z", "D", "E"),  # gap
            ]
        },
    )
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["break_reason"] == "GAP"
    assert [s["scan_id"] for s in chain["unverified_scans"]] == ["S3"]

    # withdraw the broken S3, backfill a correct one at an in-between timestamp
    client.post(
        "/api/waybills/AWB-1/scans/batch", json={"scans": [withdrawal("S3", 2)]}
    )
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S3", 3, "2026-11-18T09:30:00Z", "C", "D")]},
    )
    chain = r.json()["chain"]
    assert chain["chain_intact"] is True
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1", "S2", "S3"]
    assert chain["current_custodian"] == "D"

"""Break detection: GAP propagation and SIMULTANEOUS (no ordering guess)."""
from conftest import handover, make_waybill


def test_gap_marks_everything_after_unverified(client):
    make_waybill(client)
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "X", "C"),  # gap
                handover("S3", 1, "2026-11-18T10:00:00Z", "C", "D"),  # tainted
            ]
        },
    )
    chain = r.json()["chain"]
    assert chain["chain_intact"] is False
    assert chain["break_reason"] == "GAP"
    assert chain["break_scan_ids"] == ["S2"]
    assert "expected 'B'" in chain["break_detail"]
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1"]
    unverified = chain["unverified_scans"]
    assert [s["scan_id"] for s in unverified] == ["S2", "S3"]
    assert all(s["verified"] is False for s in unverified)
    # custodian only advances along verified scans
    assert chain["current_custodian"] == "B"


def test_simultaneous_handovers_not_ordered(client):
    make_waybill(client)
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
                handover("S3", 1, "2026-11-18T09:00:00Z", "C", "D"),  # same ts as S2
            ]
        },
    )
    chain = r.json()["chain"]
    assert chain["break_reason"] == "SIMULTANEOUS"
    assert sorted(chain["break_scan_ids"]) == ["S2", "S3"]
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1"]
    assert sorted(s["scan_id"] for s in chain["unverified_scans"]) == ["S2", "S3"]
    assert chain["current_custodian"] == "B"


def test_resolving_simultaneous_group_heals_chain(client):
    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={
            "scans": [
                handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
                handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
                handover("S3", 1, "2026-11-18T09:00:00Z", "C", "D"),
            ]
        },
    )
    # correct S3 to a later timestamp
    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S3", 2, "2026-11-18T10:00:00Z", "C", "D")]},
    )
    chain = r.json()["chain"]
    assert chain["chain_intact"] is True
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1", "S2", "S3"]
    assert chain["current_custodian"] == "D"


def test_late_backfill_before_break_extends_verified_prefix(client):
    make_waybill(client)
    client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C")]},
    )
    # S2 initially unverified: origin ORIG != B
    chain = client.get("/api/waybills/AWB-1/chain").json()
    assert chain["break_reason"] == "GAP"

    r = client.post(
        "/api/waybills/AWB-1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    chain = r.json()["chain"]
    assert chain["chain_intact"] is True
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1", "S2"]
    assert chain["current_custodian"] == "C"

"""Happy-path chain verification: trusted prefix, custodian tracking."""
from conftest import handover, make_waybill


def test_empty_waybill_chain(client):
    make_waybill(client)
    r = client.get("/api/waybills/AWB-1/chain")
    assert r.status_code == 200
    body = r.json()
    assert body["chain_intact"] is True
    assert body["break_reason"] == "NONE"
    assert body["current_custodian"] == "ORIG"
    assert body["verified_prefix"] == []
    assert body["unverified_scans"] == []


def test_continuous_handovers_verify_in_time_order(client):
    make_waybill(client)
    batch = {
        "scans": [
            handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C"),  # out of order in batch
            handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
        ]
    }
    r = client.post("/api/waybills/AWB-1/scans/batch", json=batch)
    assert r.status_code == 200, r.text
    chain = r.json()["chain"]
    assert [s["scan_id"] for s in chain["verified_prefix"]] == ["S1", "S2"]
    assert chain["current_custodian"] == "C"
    assert chain["chain_intact"] is True
    assert chain["unverified_scans"] == []


def test_timezone_offsets_normalized_to_utc(client):
    make_waybill(client)
    # 16:00+08:00 == 08:00Z
    batch = {"scans": [handover("S1", 1, "2026-11-18T16:00:00+08:00", "ORIG", "B")]}
    r = client.post("/api/waybills/AWB-1/scans/batch", json=batch)
    assert r.status_code == 200, r.text
    ts = r.json()["chain"]["verified_prefix"][0]["ts_utc"]
    assert ts.startswith("2026-11-18T08:00:00")


def test_unknown_waybill_returns_404(client):
    r = client.get("/api/waybills/NOPE/chain")
    assert r.status_code == 404
    r2 = client.post(
        "/api/waybills/NOPE/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    assert r2.status_code == 404


def test_duplicate_waybill_rejected(client):
    make_waybill(client)
    r = client.post("/api/waybills", json={"waybill_id": "AWB-1", "origin_party": "X"})
    assert r.status_code == 409

"""Route-prefix summary: aggregation semantics, incremental maintenance and
legacy-volume backfill (BUILDING -> READY), restart durability.
"""
from __future__ import annotations

import importlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import handover, make_waybill, withdrawal


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def wait_ready(client, timeout=30.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get("/api/route-summary/status")
        assert r.status_code == 200
        last = r.json()
        if last["ready"]:
            return last
        time.sleep(0.02)
    raise AssertionError(f"summary never became ready: {last}")


def route_query(client, parties):
    return client.post("/api/route-summary", json={"parties": parties})


def build_chain(client, wid, scans):
    make_waybill(client, wid, origin="ORIG")
    if scans:
        r = client.post(
            f"/api/waybills/{wid}/scans/batch", json={"scans": scans}
        )
        assert r.status_code == 200, r.text


def golden_counts(db_path, parties):
    """Independent recomputation straight from scans, bypassing the summary."""
    from app import database, services

    conn = database.connect(db_path)
    try:
        waybills = [
            r["id"]
            for r in conn.execute("SELECT id FROM waybills ORDER BY id").fetchall()
        ]
        groups: dict[str, int] = {}
        terminal = 0
        depth = len(parties)
        for wid in waybills:
            chain = services.compute_chain(conn, wid)
            custodians = [chain.origin_party] + [
                s.receiver for s in chain.verified_prefix
            ]
            if custodians[:depth] != parties:
                continue
            if len(custodians) == depth:
                terminal += 1  # trusted chain ends exactly on the queried route
            else:
                groups[custodians[depth]] = groups.get(custodians[depth], 0) + 1
        return groups, terminal
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Basic aggregation
# --------------------------------------------------------------------------- #
def test_fresh_deployment_summary_is_ready_immediately(client):
    # No legacy rows: the build finishes (is a no-op) during lifespan startup.
    state = wait_ready(client)
    assert state["status"] == "READY"
    assert state["pending_waybills"] == 0


def test_next_custodian_grouping_and_terminal_count(client):
    # W1: ORIG -> B -> C
    build_chain(
        client, "W1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C")],
    )
    # W2: ORIG -> B -> D
    build_chain(
        client, "W2",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "B", "D")],
    )
    # W3: ORIG -> B, then GAP (X -> E unverified): trusted chain ends at B
    build_chain(
        client, "W3",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "X", "E")],
    )
    # W4: ORIG only, no scans at all
    build_chain(client, "W4", [])
    # W5: ORIG -> Z (different first leg, must not match)
    build_chain(
        client, "W5",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "Z")],
    )

    wait_ready(client)
    r = route_query(client, ["ORIG", "B"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready"] is True
    assert body["route"] == ["ORIG", "B"]
    # W1, W2, W3 start with ORIG->B; W4 stops at ORIG and W5 goes to Z.
    assert body["matched_waybills"] == 3
    groups = {g["next_party"]: g["waybill_count"] for g in body["next_custodians"]}
    assert groups == {"C": 1, "D": 1}
    # W3 breaks right after B: it has a later scan but no further *verified* one.
    assert body["no_further_scan_count"] == 1

    # Depth 1: origin-only route.  W4 ends at ORIG -> terminal; the rest move.
    r = route_query(client, ["ORIG"])
    body = r.json()
    assert body["matched_waybills"] == 5
    groups = {g["next_party"]: g["waybill_count"] for g in body["next_custodians"]}
    assert groups == {"B": 3, "Z": 1}
    assert body["no_further_scan_count"] == 1


def test_each_waybill_counted_in_one_group_per_query(client):
    build_chain(
        client, "W1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
         handover("S3", 1, "2026-11-18T10:00:00Z", "C", "D")],
    )
    wait_ready(client)
    body = route_query(client, ["ORIG"]).json()
    total = sum(g["waybill_count"] for g in body["next_custodians"])
    total += body["no_further_scan_count"]
    assert total == body["matched_waybills"] == 1


def test_duplicate_custodians_in_route_are_order_significant(client):
    # ORIG -> B -> ORIG -> B (revisits parties)
    build_chain(
        client, "W1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "B", "ORIG"),
         handover("S3", 1, "2026-11-18T10:00:00Z", "ORIG", "B")],
    )
    wait_ready(client)

    # The repeated-party prefix matches and points at B again.
    body = route_query(client, ["ORIG", "B", "ORIG"]).json()
    assert body["matched_waybills"] == 1
    assert body["next_custodians"] == [{"next_party": "B", "waybill_count": 1}]
    assert body["no_further_scan_count"] == 0

    # A different ordering of the same parties must not match.
    body = route_query(client, ["ORIG", "ORIG", "B"]).json()
    assert body["matched_waybills"] == 0
    assert body["next_custodians"] == []
    assert body["no_further_scan_count"] == 0

    # Full-route query: the chain ends exactly there -> terminal.
    body = route_query(client, ["ORIG", "B", "ORIG", "B"]).json()
    assert body["matched_waybills"] == 1
    assert body["no_further_scan_count"] == 1


def test_simultaneous_break_counts_as_no_further_scan(client):
    build_chain(
        client, "W1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"),
         handover("S3", 1, "2026-11-18T09:00:00Z", "C", "D")],  # simultaneous
    )
    wait_ready(client)
    body = route_query(client, ["ORIG", "B"]).json()
    # Trusted chain ends at B; the simultaneous group is not verified.
    assert body["matched_waybills"] == 1
    assert body["no_further_scan_count"] == 1
    assert body["next_custodians"] == []


def test_unknown_route_returns_empty_groups(client):
    build_chain(client, "W1", [])
    wait_ready(client)
    body = route_query(client, ["NO-SUCH-ORIGIN"]).json()
    assert body["matched_waybills"] == 0
    assert body["next_custodians"] == []
    assert body["no_further_scan_count"] == 0


def test_route_query_validation(client):
    wait_ready(client)
    assert route_query(client, []).status_code == 422
    assert route_query(client, ["ORIG", "  "]).status_code == 422
    r = client.post("/api/route-summary", json={"parties": ["ORIG"], "extra": 1})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Incremental updates: late reports, corrections and withdrawals only touch
# the waybill's own prefix contributions.
# --------------------------------------------------------------------------- #
def test_late_backfill_extends_summary_contributions(client):
    build_chain(
        client, "W1",
        [handover("S2", 1, "2026-11-18T10:00:00Z", "B", "C")],  # GAP initially
    )
    wait_ready(client)
    # Trusted chain is just ORIG: querying ORIG -> terminal (break right away).
    body = route_query(client, ["ORIG"]).json()
    assert body["no_further_scan_count"] == 1

    r = client.post(
        "/api/waybills/W1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]},
    )
    assert r.status_code == 200
    body = route_query(client, ["ORIG"]).json()
    assert {g["next_party"]: g["waybill_count"]
            for g in body["next_custodians"]} == {"B": 1}
    assert body["no_further_scan_count"] == 0
    body = route_query(client, ["ORIG", "B"]).json()
    assert {g["next_party"]: g["waybill_count"]
            for g in body["next_custodians"]} == {"C": 1}


def test_correction_that_breaks_earlier_removes_old_destinations(client):
    # Initially intact: ORIG -> B -> C.
    build_chain(
        client, "W1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C")],
    )
    wait_ready(client)
    assert route_query(client, ["ORIG", "B"]).json()["next_custodians"] == [
        {"next_party": "C", "waybill_count": 1}
    ]

    # Correct S1 to a bad giver: the trusted chain now ends at ORIG, so the
    # contribution under prefix [ORIG, B] and the C destination must vanish.
    r = client.post(
        "/api/waybills/W1/scans/batch",
        json={"scans": [handover("S1", 2, "2026-11-18T08:00:00Z", "WRONG", "B")]},
    )
    assert r.status_code == 200
    body = route_query(client, ["ORIG"]).json()
    assert body["no_further_scan_count"] == 1
    assert body["next_custodians"] == []
    # The old destination and the deeper prefix contribution are both removed.
    body = route_query(client, ["ORIG", "B"]).json()
    assert body["matched_waybills"] == 0
    body = route_query(client, ["ORIG", "B", "C"]).json()
    assert body["matched_waybills"] == 0


def test_late_backfill_that_breaks_early_deducts_old_destinations(client):
    # Intact chain O -> A -> B -> C.
    build_chain(
        client, "W1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "A"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "A", "B"),
         handover("S3", 1, "2026-11-18T10:00:00Z", "B", "C")],
    )
    wait_ready(client)
    body = route_query(client, ["ORIG", "A"]).json()
    assert {g["next_party"]: g["waybill_count"]
            for g in body["next_custodians"]} == {"B": 1}
    assert route_query(client, ["ORIG", "A", "B", "C"]).json()["matched_waybills"] == 1

    # A late report at an earlier timestamp with a bad giver breaks the trusted
    # chain at the origin: the old destination B and every deeper prefix the
    # waybill used to contribute to must be deducted.
    r = client.post(
        "/api/waybills/W1/scans/batch",
        json={"scans": [handover("S0", 1, "2026-11-18T07:00:00Z", "WRONG", "Z")]},
    )
    assert r.status_code == 200
    body = route_query(client, ["ORIG"]).json()
    assert body["next_custodians"] == []
    assert body["no_further_scan_count"] == 1  # chain ends immediately at origin
    for prefix in (["ORIG", "A"], ["ORIG", "A", "B"], ["ORIG", "A", "B", "C"]):
        assert route_query(client, prefix).json()["matched_waybills"] == 0

    # Withdrawing the offending late scan restores every prior contribution.
    r = client.post(
        "/api/waybills/W1/scans/batch",
        json={"scans": [withdrawal("S0", 2)]},
    )
    assert r.status_code == 200
    body = route_query(client, ["ORIG", "A"]).json()
    assert {g["next_party"]: g["waybill_count"]
            for g in body["next_custodians"]} == {"B": 1}
    assert route_query(client, ["ORIG", "A", "B", "C"]).json()["no_further_scan_count"] == 1


def test_withdrawal_shrinks_contributions(client):
    build_chain(
        client, "W1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C")],
    )
    wait_ready(client)
    r = client.post(
        "/api/waybills/W1/scans/batch",
        json={"scans": [withdrawal("S2", 2)]},
    )
    assert r.status_code == 200
    body = route_query(client, ["ORIG"]).json()
    assert {g["next_party"]: g["waybill_count"]
            for g in body["next_custodians"]} == {"B": 1}
    # Chain now ends at B.
    body = route_query(client, ["ORIG", "B"]).json()
    assert body["matched_waybills"] == 1
    assert body["no_further_scan_count"] == 1


def test_failed_batch_keeps_old_counts(client):
    build_chain(
        client, "W1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
    )
    wait_ready(client)
    before = route_query(client, ["ORIG"]).json()
    # Same-revision divergence -> whole batch rollback.
    r = client.post(
        "/api/waybills/W1/scans/batch",
        json={"scans": [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "TAMPERED")]},
    )
    assert r.status_code == 409
    after = route_query(client, ["ORIG"]).json()
    assert after == before


def test_summary_matches_full_recomputation_for_many_waybills(client):
    from app import config

    for i in range(25):
        wid = f"W{i:03d}"
        make_waybill(client, wid, origin="ORIG")
        scans = [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")]
        if i % 2 == 0:
            scans.append(handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C"))
        if i % 5 == 0:  # some chains break at the third handover
            scans.append(handover("S3", 1, "2026-11-18T10:00:00Z", "X", "Y"))
        if i % 7 == 0:  # some stay at origin with an immediate GAP
            client.post(
                f"/api/waybills/{wid}/scans/batch",
                json={"scans": [handover("G", 1, "2026-11-18T07:00:00Z", "Z", "Q")]},
            )
            continue
        client.post(f"/api/waybills/{wid}/scans/batch", json={"scans": scans})

    wait_ready(client)
    for parties in (["ORIG"], ["ORIG", "B"], ["ORIG", "B", "C"]):
        body = route_query(client, parties).json()
        golden_groups, golden_terminal = golden_counts(config.DB_PATH, parties)
        got_groups = {g["next_party"]: g["waybill_count"]
                      for g in body["next_custodians"]}
        assert got_groups == golden_groups, parties
        assert body["no_further_scan_count"] == golden_terminal, parties
        assert body["matched_waybills"] == (
            sum(golden_groups.values()) + golden_terminal
        )


# --------------------------------------------------------------------------- #
# Legacy-volume backfill
# --------------------------------------------------------------------------- #
def _wipe_summary_tables(db_path):
    from app import database

    conn = database.connect(db_path)
    try:
        conn.execute("DELETE FROM route_prefix_contribs")
        conn.execute("DELETE FROM waybill_chain_versions")
        conn.execute("DELETE FROM summary_state")
        conn.commit()
    finally:
        conn.close()


def test_backfill_serves_503_until_ready_and_then_matches(client, monkeypatch):
    from app import config, summary

    # Pre-existing waybills on an "old" volume (summary tables absent).
    for i in range(20):
        wid = f"LEG{i:02d}"
        build_chain(
            client, wid,
            [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
             handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C")],
        )
    _wipe_summary_tables(config.DB_PATH)

    # Slow the background build down so BUILDING is observable.
    monkeypatch.setattr(config, "BACKFILL_BATCH_SIZE", 1)
    monkeypatch.setattr(config, "BACKFILL_IDLE_SECONDS", 0.1)
    summary.stop_backfill()
    summary.initialize_summary(config.DB_PATH)

    status = client.get("/api/route-summary/status").json()
    assert status["ready"] is False
    assert status["status"] == "BUILDING"
    assert status["pending_waybills"] == 20

    r = route_query(client, ["ORIG", "B"])
    assert r.status_code == 503
    assert r.json()["status"] == "BUILDING"
    assert r.json()["ready"] is False

    final = wait_ready(client)
    assert final["pending_waybills"] == 0

    body = route_query(client, ["ORIG", "B"]).json()
    groups = {g["next_party"]: g["waybill_count"] for g in body["next_custodians"]}
    assert groups == {"C": 20}
    golden_groups, golden_terminal = golden_counts(config.DB_PATH, ["ORIG", "B"])
    assert groups == golden_groups
    assert body["no_further_scan_count"] == golden_terminal


def test_changes_committed_during_backfill_are_counted_at_first_ready(
    client, monkeypatch
):
    from app import config, summary

    for i in range(30):
        build_chain(
            client, f"LEG{i:02d}",
            [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B")],
        )
    _wipe_summary_tables(config.DB_PATH)

    monkeypatch.setattr(config, "BACKFILL_BATCH_SIZE", 2)
    monkeypatch.setattr(config, "BACKFILL_IDLE_SECONDS", 0.05)
    summary.stop_backfill()
    summary.initialize_summary(config.DB_PATH)

    # While the backfill is running, the app must still accept waybills and
    # scans; those writes maintain the summary inline.
    saw_building = False

    def writer():
        nonlocal saw_building
        for i in range(5):
            wid = f"NEW{i}"
            make_waybill(client, wid, origin="ORIG")
            r = client.post(
                f"/api/waybills/{wid}/scans/batch",
                json={"scans": [
                    handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "NEW-PARTY")
                ]},
            )
            assert r.status_code == 200
            if client.get("/api/route-summary/status").json()["status"] == "BUILDING":
                saw_building = True
            time.sleep(0.02)

    thread = threading.Thread(target=writer)
    thread.start()
    thread.join(timeout=30)
    assert saw_building, "writer did not overlap with the backfill window"

    wait_ready(client)
    body = route_query(client, ["ORIG"]).json()
    groups = {g["next_party"]: g["waybill_count"] for g in body["next_custodians"]}
    # 30 legacy -> B, 5 created during build -> NEW-PARTY.
    assert groups == {"B": 30, "NEW-PARTY": 5}
    golden_groups, _ = golden_counts(config.DB_PATH, ["ORIG"])
    assert groups == golden_groups


def test_ready_summary_survives_restart_and_needs_no_rebuild(client):
    from app import config, main, summary

    build_chain(
        client, "W1",
        [handover("S1", 1, "2026-11-18T08:00:00Z", "ORIG", "B"),
         handover("S2", 1, "2026-11-18T09:00:00Z", "B", "C")],
    )
    wait_ready(client)
    summary.stop_backfill()

    # Simulate a process restart against the same SQLite file.
    importlib.reload(main)
    with TestClient(main.app) as new_client:
        status = new_client.get("/api/route-summary/status").json()
        assert status["ready"] is True
        assert status["status"] == "READY"
        assert status["pending_waybills"] == 0
        body = route_query(new_client, ["ORIG", "B"]).json()
        assert {g["next_party"]: g["waybill_count"]
                for g in body["next_custodians"]} == {"C": 1}
    summary.stop_backfill()
    assert config.DB_PATH.exists()

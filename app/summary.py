"""Materialized route-prefix summary and its legacy backfill.

Why a materialized summary
--------------------------
The freight terminal asks: *among waybills whose trusted custody chain starts
with a given ordered custodian route, where did each one go next, and how many
have no further scan / break right there?*  Answering that by recomputing every
waybill's chain on each query does not scale, so each waybill's contribution to
every prefix of its verified chain is stored once in
``route_prefix_contribs`` and a query becomes a single ``GROUP BY``.

Consistency model
-----------------
* Waybill creation and every scan batch bump ``chain_version`` and replace that
  waybill's contribution rows **inside the same ``BEGIN IMMEDIATE``
  transaction** as the scan writes.  Scans and the counts they drive therefore
  become visible atomically; a rolled-back batch leaves the old summary intact
  ("failure keeps the old result").
* A backfill thread reuses the exact same per-waybill rebuild routine for
  waybills already present in a pre-existing SQLite volume.  While any waybill
  is pending, queries report ``status=BUILDING`` (HTTP 503).  The build state
  is persisted in ``summary_state`` so a restart of a READY volume serves
  queries immediately.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from . import config
from .database import connect, transaction
from .schemas import utcnow_iso

# Terminal marker used inside route_prefix_contribs.next_party.  Custodian
# names are stripped non-empty strings, so '' can never collide with a real
# party; it is translated back to null at the API boundary.
TERMINAL_PARTY = ""

_backfill_lock = threading.Lock()
_backfill_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None


# --------------------------------------------------------------------------- #
# Canonical prefix encoding
# --------------------------------------------------------------------------- #
def encode_prefix(parties: list[str]) -> str:
    """Deterministic serialization of an ordered custodian list.

    Each party is length-prefixed so that separators inside a name can never
    make two different lists share an encoding.
    """
    return "\x1f".join(f"{len(p)}:{p}" for p in parties)


# --------------------------------------------------------------------------- #
# State helpers
# --------------------------------------------------------------------------- #
def _ensure_state_row(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT status FROM summary_state WHERE id = 1").fetchone()
    if row is None:
        # First startup against a volume created before the summary feature:
        # assume a backfill is required (initialize_summary flips it to READY
        # immediately when there is in fact nothing pending).
        conn.execute(
            "INSERT INTO summary_state (id, status, updated_at) VALUES (1, 'BUILDING', ?)",
            (utcnow_iso(),),
        )
        return "BUILDING"
    return row["status"]


def get_status(conn: sqlite3.Connection) -> dict:
    """Return build status and the number of waybills still pending."""
    row = conn.execute("SELECT status FROM summary_state WHERE id = 1").fetchone()
    if row is None:
        # Fresh process hitting a brand-new DB before lifespan ran init.
        return {"status": "BUILDING", "ready": False, "pending_waybills": None}
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM waybill_chain_versions "
        "WHERE materialized_version IS NULL OR materialized_version < chain_version"
    ).fetchone()["n"]
    status = row["status"]
    return {
        "status": status,
        "ready": status == "READY" and pending == 0,
        "pending_waybills": pending,
    }


# --------------------------------------------------------------------------- #
# Per-waybill contribution computation
# --------------------------------------------------------------------------- #
def _verified_chain(conn: sqlite3.Connection, waybill_id: str) -> tuple[str, list[sqlite3.Row]]:
    """Trusted chain of one waybill: (origin_party, verified handovers in order).

    Mirrors services.compute_chain's verification rules (time ordering,
    SIMULTANEOUS groups, GAP detection) but stops at the first break, since the
    summary only concerns the trusted prefix.
    """
    from .services import _adopted_handovers  # local import: avoid import cycle

    origin = conn.execute(
        "SELECT origin_party FROM waybills WHERE id = ?", (waybill_id,)
    ).fetchone()["origin_party"]

    rows = _adopted_handovers(conn, waybill_id)
    groups: list[list[sqlite3.Row]] = []
    for row in rows:
        if groups and groups[-1][0]["ts_utc"] == row["ts_utc"]:
            groups[-1].append(row)
        else:
            groups.append([row])

    verified: list[sqlite3.Row] = []
    custodian = origin
    for group in groups:
        if len(group) > 1:
            break  # SIMULTANEOUS: order cannot be determined, chain ends here
        scan = group[0]
        if scan["giver"] != custodian:
            break  # GAP: trusted chain ends immediately before this scan
        verified.append(scan)
        custodian = scan["receiver"]
    return origin, verified


def _contribution_rows(
    waybill_id: str, origin: str, verified: list[sqlite3.Row], version: int
) -> list[tuple]:
    """Build the (waybill, prefix_depth, prefix_key, next_party, version) rows.

    With custodians c0=origin, c1, ..., cd (d verified handovers), depth k
    (1-based) covers the route [c0 .. c_{k-1}] and records where the waybill
    went next (c_k).  The terminal row records that no further verified
    handover exists after [c0 .. c_d] -- either there are no more scans at
    all, or the chain breaks (GAP / SIMULTANEOUS) at the next position.
    """
    custodians = [origin] + [r["receiver"] for r in verified]
    rows: list[tuple] = []
    for depth in range(1, len(custodians) + 1):
        prefix = custodians[:depth]
        if depth < len(custodians):
            next_party = custodians[depth]
        else:
            next_party = TERMINAL_PARTY  # no further verified handover
        rows.append(
            (waybill_id, depth, encode_prefix(prefix), next_party, version)
        )
    return rows


def rebuild_waybill_summary(
    conn: sqlite3.Connection,
    waybill_id: str,
    *,
    bump_version: bool,
) -> int:
    """Replace one waybill's prefix contributions and advance its version.

    Called in two contexts:

    * online write path (``bump_version=True``): the scan batch already
      committed logically inside the caller's transaction; the version moves
      forward together with the new counts.
    * legacy backfill (``bump_version=False``): catch ``materialized_version``
      up to whatever ``chain_version`` the waybill currently has without
      fabricating a version change.

    Returns the chain_version the contribution rows now reflect.
    """
    version_row = conn.execute(
        "SELECT chain_version FROM waybill_chain_versions WHERE waybill_id = ?",
        (waybill_id,),
    ).fetchone()
    if version_row is None:
        # Legacy waybill never registered yet (backfill startup race).
        conn.execute(
            "INSERT INTO waybill_chain_versions "
            "(waybill_id, chain_version, materialized_version, updated_at) "
            "VALUES (?, 0, NULL, ?)",
            (waybill_id, utcnow_iso()),
        )
        current_version = 0
    else:
        current_version = version_row["chain_version"]

    new_version = current_version + 1 if bump_version else current_version

    origin, verified = _verified_chain(conn, waybill_id)
    rows = _contribution_rows(waybill_id, origin, verified, new_version)

    conn.execute(
        "DELETE FROM route_prefix_contribs WHERE waybill_id = ?", (waybill_id,)
    )
    conn.executemany(
        "INSERT INTO route_prefix_contribs "
        "(waybill_id, prefix_depth, prefix_key, next_party, chain_version) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute(
        "UPDATE waybill_chain_versions SET chain_version = ?, "
        "materialized_version = ?, updated_at = ? WHERE waybill_id = ?",
        (new_version, new_version, utcnow_iso(), waybill_id),
    )
    return new_version


# --------------------------------------------------------------------------- #
# Query path: one GROUP BY over the committed summary
# --------------------------------------------------------------------------- #
def query_route_summary(
    conn: sqlite3.Connection, parties: list[str]
) -> dict:
    """Aggregate next-custodian counts for one ordered custodian route.

    Raises ``SummaryNotReady`` while the legacy backfill is incomplete.
    Each waybill contributes to at most one group of this query, so the
    per-group counts are disjoint waybill sets.
    """
    status = get_status(conn)
    if not status["ready"]:
        raise SummaryNotReady(
            f"route summary not ready yet: status={status['status']}, "
            f"pending_waybills={status['pending_waybills']}"
        )

    depth = len(parties)
    key = encode_prefix(parties)
    grouped = conn.execute(
        "SELECT next_party, COUNT(*) AS n "
        "FROM route_prefix_contribs "
        "WHERE prefix_depth = ? AND prefix_key = ? "
        "GROUP BY next_party ORDER BY next_party",
        (depth, key),
    ).fetchall()

    next_custodians: list[dict] = []
    no_further_scan = 0
    matched = 0
    for row in grouped:
        matched += row["n"]
        if row["next_party"] == TERMINAL_PARTY:
            no_further_scan = row["n"]
        else:
            next_custodians.append(
                {"next_party": row["next_party"], "waybill_count": row["n"]}
            )
    next_custodians.sort(key=lambda g: g["next_party"])

    return {
        "ready": True,
        "status": "READY",
        "route": parties,
        "matched_waybills": matched,
        "next_custodians": next_custodians,
        "no_further_scan_count": no_further_scan,
    }


class SummaryNotReady(Exception):
    """HTTP 503: backfill of the legacy volume has not finished."""


# --------------------------------------------------------------------------- #
# Legacy backfill
# --------------------------------------------------------------------------- #
def _pending_waybill_ids(conn: sqlite3.Connection, limit: int) -> list[str]:
    return [
        r["id"]
        for r in conn.execute(
            "SELECT w.id FROM waybills AS w "
            "LEFT JOIN waybill_chain_versions AS v ON v.waybill_id = w.id "
            "WHERE v.waybill_id IS NULL OR v.materialized_version IS NULL "
            "OR v.materialized_version < v.chain_version "
            "ORDER BY w.id LIMIT ?",
            (limit,),
        ).fetchall()
    ]


def run_backfill_once(db_path, batch_size: int | None = None,
                      stop_event: threading.Event | None = None) -> int:
    """Drain all pending waybills.  Returns the number processed."""
    batch_size = batch_size or config.BACKFILL_BATCH_SIZE
    conn = connect(db_path)
    processed = 0
    try:
        while stop_event is None or not stop_event.is_set():
            with transaction(conn):
                ids = _pending_waybill_ids(conn, batch_size)
                if not ids:
                    conn.execute(
                        "UPDATE summary_state SET status = 'READY', updated_at = ? "
                        "WHERE id = 1",
                        (utcnow_iso(),),
                    )
                    break
                for waybill_id in ids:
                    rebuild_waybill_summary(
                        conn, waybill_id, bump_version=False
                    )
                    processed += 1
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(config.BACKFILL_IDLE_SECONDS)
    finally:
        conn.close()
    return processed


def start_backfill(db_path) -> None:
    """Launch the daemon backfill thread unless one is already running."""
    global _backfill_thread, _stop_event
    with _backfill_lock:
        if _backfill_thread is not None and _backfill_thread.is_alive():
            return
        _stop_event = threading.Event()

        def _runner() -> None:
            try:
                run_backfill_once(db_path, stop_event=_stop_event)
            except Exception:  # pragma: no cover - surfaced via /status
                import logging

                logging.getLogger("app.summary").exception(
                    "route-summary backfill failed; status stays BUILDING"
                )

        thread = threading.Thread(
            target=_runner, name="route-summary-backfill", daemon=True
        )
        _backfill_thread = thread
        thread.start()


def stop_backfill() -> None:
    """Signal the backfill to stop (used on app shutdown and test teardown)."""
    global _backfill_thread, _stop_event
    with _backfill_lock:
        if _stop_event is not None:
            _stop_event.set()
        thread = _backfill_thread
        _backfill_thread = None
        _stop_event = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)


def initialize_summary(db_path) -> None:
    """Idempotent startup: create the state row and decide on backfill."""
    conn = connect(db_path)
    try:
        with transaction(conn):
            status = _ensure_state_row(conn)
            # Legacy waybills predate the version table: register them as
            # pending so the backfill has concrete work items.
            conn.execute(
                "INSERT OR IGNORE INTO waybill_chain_versions "
                "(waybill_id, chain_version, materialized_version, updated_at) "
                "SELECT w.id, 0, NULL, ? FROM waybills AS w "
                "WHERE NOT EXISTS (SELECT 1 FROM waybill_chain_versions v "
                "                  WHERE v.waybill_id = w.id)",
                (utcnow_iso(),),
            )
            pending = _pending_waybill_ids(conn, 1)
            if not pending:
                conn.execute(
                    "UPDATE summary_state SET status = 'READY', updated_at = ? "
                    "WHERE id = 1",
                    (utcnow_iso(),),
                )
                ready = True
            else:
                ready = False
    finally:
        conn.close()
    if not ready and status != "READY":
        start_backfill(db_path)


__all__ = [
    "SummaryNotReady",
    "TERMINAL_PARTY",
    "encode_prefix",
    "get_status",
    "initialize_summary",
    "query_route_summary",
    "rebuild_waybill_summary",
    "run_backfill_once",
    "start_backfill",
    "stop_backfill",
]

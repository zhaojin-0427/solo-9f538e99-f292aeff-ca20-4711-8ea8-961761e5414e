"""Domain logic: revision arbitration, atomic batch apply, chain recomputation.

All write paths run inside one ``BEGIN IMMEDIATE`` transaction.  Any rule
violation raises ``DomainConflict`` / ``DomainValidation`` and rolls the whole
batch back, so the previously committed chain is preserved untouched.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .schemas import (
    BatchResponse,
    BatchSubmit,
    BreakReason,
    ChainResponse,
    ScanEntry,
    ScanView,
    WaybillCreate,
    entry_payload_hash,
    to_utc_iso,
    utcnow_iso,
)

_STORAGE_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


class DomainConflict(Exception):
    """HTTP 409: same-revision divergence, stale revision, unknown scan ..."""


class DomainValidation(Exception):
    """HTTP 404: referenced waybill/resource does not exist."""


# --------------------------------------------------------------------------- #
# Waybills
# --------------------------------------------------------------------------- #
def create_waybill(conn: sqlite3.Connection, body: WaybillCreate) -> dict:
    from .database import transaction

    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM waybills WHERE id = ?", (body.waybill_id,)
        ).fetchone()
        if existing is not None:
            raise DomainConflict(f"waybill {body.waybill_id!r} already exists")
        conn.execute(
            "INSERT INTO waybills (id, origin_party, created_at) VALUES (?, ?, ?)",
            (body.waybill_id, body.origin_party, utcnow_iso()),
        )
    return {"waybill_id": body.waybill_id, "origin_party": body.origin_party}


def get_waybill(conn: sqlite3.Connection, waybill_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, origin_party FROM waybills WHERE id = ?", (waybill_id,)
    ).fetchone()
    if row is None:
        raise DomainValidation(f"waybill {waybill_id!r} not found")
    return row


# --------------------------------------------------------------------------- #
# Batch submission (handover reports, corrections, withdrawals)
# --------------------------------------------------------------------------- #
def apply_batch(
    conn: sqlite3.Connection, waybill_id: str, batch: BatchSubmit
) -> BatchResponse:
    from .database import transaction

    get_waybill(conn, waybill_id)  # 404-style check before opening the txn

    applied = 0
    idempotent = 0
    withdrawn: list[str] = []

    with transaction(conn):
        for entry in batch.scans:
            wrote = _apply_one(conn, waybill_id, entry)
            if not wrote:
                idempotent += 1
                continue
            if entry.event_type == "WITHDRAWAL":
                withdrawn.append(entry.scan_id)
            applied += 1

    # Recompute against the freshly committed snapshot.  Scan writes and the
    # returned query result therefore become visible together.
    chain = compute_chain(conn, waybill_id)
    return BatchResponse(
        waybill_id=waybill_id,
        applied=applied,
        idempotent_duplicates=idempotent,
        withdrawn_scan_ids=withdrawn,
        chain=chain,
    )


def _apply_one(conn: sqlite3.Connection, waybill_id: str, entry: ScanEntry) -> bool:
    """Insert one scan revision.  Returns False when it was an idempotent no-op."""
    h = entry_payload_hash(entry)

    newest = conn.execute(
        "SELECT revision, event_type, payload_hash "
        "FROM scans WHERE waybill_id = ? AND scan_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (waybill_id, entry.scan_id),
    ).fetchone()

    if newest is not None and newest["revision"] > entry.revision:
        raise DomainConflict(
            f"scan_id={entry.scan_id!r} revision={entry.revision} is stale: "
            f"revision {newest['revision']} already adopted"
        )

    same_rev = conn.execute(
        "SELECT event_type, payload_hash FROM scans "
        "WHERE waybill_id = ? AND scan_id = ? AND revision = ?",
        (waybill_id, entry.scan_id, entry.revision),
    ).fetchone()

    if same_rev is not None:
        # 同修订异文 -> 整批拒绝；同文 -> 幂等跳过
        if same_rev["payload_hash"] != h:
            raise DomainConflict(
                f"scan_id={entry.scan_id!r} revision={entry.revision} already "
                "exists with different content (same-revision divergence)"
            )
        return False

    if entry.event_type == "WITHDRAWAL" and newest is None:
        raise DomainConflict(
            f"cannot withdraw unknown scan_id={entry.scan_id!r}: no prior revision exists"
        )

    ts_text = to_utc_iso(entry.ts_utc) if entry.ts_utc is not None else None
    conn.execute(
        "INSERT INTO scans (waybill_id, scan_id, revision, event_type, "
        "ts_utc, giver, receiver, payload_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            waybill_id,
            entry.scan_id,
            entry.revision,
            entry.event_type,
            ts_text,
            entry.giver,
            entry.receiver,
            h,
            utcnow_iso(),
        ),
    )
    return True


# --------------------------------------------------------------------------- #
# Chain recomputation
# --------------------------------------------------------------------------- #
def _adopted_handovers(
    conn: sqlite3.Connection, waybill_id: str
) -> list[sqlite3.Row]:
    """Return handover rows for each scan's highest revision, time-ordered.

    Scans whose highest revision is a WITHDRAWAL tombstone do not appear.
    """
    return conn.execute(
        """
        SELECT s.scan_id, s.revision, s.event_type, s.ts_utc, s.giver, s.receiver
        FROM scans AS s
        JOIN (
            SELECT scan_id, MAX(revision) AS max_revision
            FROM scans
            WHERE waybill_id = ?
            GROUP BY scan_id
        ) AS m
          ON m.scan_id = s.scan_id AND m.max_revision = s.revision
        WHERE s.waybill_id = ? AND s.event_type = 'HANDOVER'
        ORDER BY s.ts_utc ASC, s.scan_id ASC
        """,
        (waybill_id, waybill_id),
    ).fetchall()


def _parse_ts(text: str) -> datetime:
    return datetime.strptime(text, _STORAGE_FMT).replace(tzinfo=timezone.utc)


def compute_chain(conn: sqlite3.Connection, waybill_id: str) -> ChainResponse:
    waybill = get_waybill(conn, waybill_id)
    origin = waybill["origin_party"]

    rows = _adopted_handovers(conn, waybill_id)

    # Group by timestamp: simultaneous handovers cannot be ordered by guessing.
    groups: list[tuple[str, list[sqlite3.Row]]] = []
    for row in rows:
        if groups and groups[-1][0] == row["ts_utc"]:
            groups[-1][1].append(row)
        else:
            groups.append((row["ts_utc"], [row]))

    verified: list[ScanView] = []
    unverified: list[ScanView] = []
    custodian = origin
    broken = False
    break_reason = BreakReason.none
    break_detail: str | None = None
    break_scan_ids: list[str] = []

    def view(row: sqlite3.Row, ok: bool, reason: BreakReason | None) -> ScanView:
        return ScanView(
            scan_id=row["scan_id"],
            revision=row["revision"],
            event_type=row["event_type"],
            ts_utc=_parse_ts(row["ts_utc"]),
            giver=row["giver"],
            receiver=row["receiver"],
            verified=ok,
            unverified_reason=reason,
        )

    for ts, group in groups:
        if broken:
            # 断链之后的扫描一律未证实，直到补报/更正/撤回补齐缺口
            unverified.extend(view(r, False, break_reason) for r in group)
            continue

        if len(group) > 1:
            broken = True
            break_reason = BreakReason.simultaneous
            break_scan_ids = [r["scan_id"] for r in group]
            break_detail = (
                f"{len(group)} handovers share ts_utc={ts}; order cannot be "
                "determined, so none of them is verified"
            )
            unverified.extend(view(r, False, BreakReason.simultaneous) for r in group)
            continue

        scan = group[0]
        if scan["giver"] != custodian:
            broken = True
            break_reason = BreakReason.gap
            break_scan_ids = [scan["scan_id"]]
            break_detail = (
                f"giver {scan['giver']!r} does not hold custody; expected "
                f"{custodian!r} (previous receiver / origin party)"
            )
            unverified.append(view(scan, False, BreakReason.gap))
            continue

        verified.append(view(scan, True, None))
        custodian = scan["receiver"]

    return ChainResponse(
        waybill_id=waybill_id,
        origin_party=origin,
        current_custodian=custodian,
        verified_prefix=verified,
        unverified_scans=unverified,
        chain_intact=not broken,
        break_reason=break_reason,
        break_detail=break_detail,
        break_scan_ids=break_scan_ids,
    )


# Re-export so other modules can reuse the domain exceptions.
__all__ = [
    "DomainConflict",
    "DomainValidation",
    "apply_batch",
    "compute_chain",
    "create_waybill",
    "get_waybill",
]

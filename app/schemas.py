"""Pydantic request/response models and payload canonicalization."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class WaybillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    waybill_id: str = Field(min_length=1, description="运单号，全局唯一")
    origin_party: str = Field(min_length=1, description="固定起始保管方")

    @field_validator("waybill_id", "origin_party")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v


class ScanEntry(BaseModel):
    """One scan inside a batch submission.

    ``event_type`` defaults to HANDOVER so the common case only carries
    scan_id / ts_utc / giver / receiver / revision.
    """

    model_config = ConfigDict(extra="forbid")

    scan_id: str = Field(min_length=1)
    revision: int = Field(gt=0, description="正整数修订号；同 ID 仅最高修订生效")
    event_type: Literal["HANDOVER", "WITHDRAWAL"] = "HANDOVER"
    ts_utc: datetime | None = Field(
        default=None, description="ISO-8601，必须带时区偏移（推荐 UTC，如 2026-11-18T08:30:00Z）"
    )
    giver: str | None = None
    receiver: str | None = None

    @field_validator("scan_id")
    @classmethod
    def _strip_scan_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("giver", "receiver")
    @classmethod
    def _strip_parties(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("ts_utc")
    @classmethod
    def _require_timezone(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("ts_utc must carry an explicit timezone offset, e.g. ...Z or +08:00")
        return v

    @model_validator(mode="after")
    def _check_shape(self) -> "ScanEntry":
        if self.event_type == "HANDOVER":
            missing = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is None
            ]
            if missing:
                raise ValueError(f"HANDOVER entry requires fields: {', '.join(missing)}")
        else:  # WITHDRAWAL: only id + higher revision are accepted
            extras = [
                name
                for name, val in (
                    ("ts_utc", self.ts_utc),
                    ("giver", self.giver),
                    ("receiver", self.receiver),
                )
                if val is not None
            ]
            if extras:
                raise ValueError(
                    f"WITHDRAWAL entry must only carry scan_id and revision; got extra: {', '.join(extras)}"
                )
        return self


class BatchSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scans: list[ScanEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_batch_duplicates(self) -> "BatchSubmit":
        # Same revision appearing twice in one batch with different text is a
        # whole-batch rejection; identical text is treated as idempotent.
        seen: dict[tuple[str, int], str] = {}
        for entry in self.scans:
            key = (entry.scan_id, entry.revision)
            h = entry_payload_hash(entry)
            if key in seen and seen[key] != h:
                raise ValueError(
                    f"scan_id={key[0]} revision={key[1]} appears multiple times "
                    "with conflicting content in the same batch"
                )
            seen[key] = h
        return self


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class BreakReason(str, Enum):
    none = "NONE"
    gap = "GAP"  # 保管方不承接：giver != 前一 receiver
    simultaneous = "SIMULTANEOUS"  # 同一时刻多笔交接，顺序不可判定


class ScanView(BaseModel):
    scan_id: str
    revision: int
    event_type: Literal["HANDOVER", "WITHDRAWAL"]
    ts_utc: datetime | None
    giver: str | None
    receiver: str | None
    verified: bool
    unverified_reason: BreakReason | None = None


class ChainResponse(BaseModel):
    waybill_id: str
    origin_party: str
    current_custodian: str
    verified_prefix: list[ScanView]
    unverified_scans: list[ScanView]
    chain_intact: bool
    break_reason: BreakReason
    break_detail: str | None = None
    break_scan_ids: list[str] = Field(default_factory=list)


class BatchResponse(BaseModel):
    waybill_id: str
    applied: int
    idempotent_duplicates: int
    withdrawn_scan_ids: list[str]
    chain: ChainResponse


# --------------------------------------------------------------------------- #
# Route-prefix summary
# --------------------------------------------------------------------------- #
class RouteQuery(BaseModel):
    """Ordered custodian list starting at the origin custodian.

    Duplicate custodians are allowed (a route may revisit a party); each
    waybill is counted in at most one group of the resulting aggregation.
    """

    model_config = ConfigDict(extra="forbid")

    parties: list[str] = Field(
        min_length=1,
        description="从起始保管方开始的有序保管方名单，可包含重复保管方",
    )

    @field_validator("parties")
    @classmethod
    def _strip_each_party(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("each party must be a string")
            value = value.strip()
            if not value:
                raise ValueError("parties must not contain empty custodian names")
            cleaned.append(value)
        return cleaned


class NextCustodianCount(BaseModel):
    next_party: str
    waybill_count: int


class RouteSummaryResponse(BaseModel):
    ready: bool
    status: Literal["READY", "BUILDING"]
    route: list[str]
    matched_waybills: int = Field(
        description="命中该路线前缀的运单总数（各下一保管方分组互不相交）"
    )
    next_custodians: list[NextCustodianCount] = Field(
        description="按下一保管方分组的运单数；在此处无后续可信交接的单不计入"
    )
    no_further_scan_count: int = Field(
        description="在此处无后续扫描或紧接着断链（可信链到此结束）的运单数"
    )


class SummaryStatusResponse(BaseModel):
    ready: bool
    status: Literal["READY", "BUILDING"]
    pending_waybills: int | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_utc_iso(dt: datetime) -> str:
    """Normalize any timezone-aware datetime to a fixed-width UTC ISO string."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_payload(entry: ScanEntry) -> dict:
    """Deterministic representation used to detect same-revision divergence."""
    if entry.event_type == "WITHDRAWAL":
        return {"event_type": "WITHDRAWAL"}
    return {
        "event_type": "HANDOVER",
        "ts_utc": to_utc_iso(entry.ts_utc),  # type: ignore[arg-type]
        "giver": entry.giver,
        "receiver": entry.receiver,
    }


def payload_hash_of(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def entry_payload_hash(entry: ScanEntry) -> str:
    return payload_hash_of(canonical_payload(entry))

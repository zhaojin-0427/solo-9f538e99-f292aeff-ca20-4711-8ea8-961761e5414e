"""Pytest fixtures: isolated temp DB + TestClient for every test."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test_chain.db"
    monkeypatch.setenv("APP_DB_PATH", str(db_path))
    monkeypatch.setenv("APP_DATA_DIR", tmpdir)

    # Import after env vars are set so config picks up the temp path.
    import importlib

    from app import config, database, main

    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(main)

    database.init_db()
    with TestClient(main.app) as c:
        yield c


def make_waybill(client, wid="AWB-1", origin="ORIG"):
    r = client.post("/api/waybills", json={"waybill_id": wid, "origin_party": origin})
    assert r.status_code == 201, r.text
    return r.json()


def handover(scan_id, revision, ts, giver, receiver):
    return {
        "scan_id": scan_id,
        "revision": revision,
        "ts_utc": ts,
        "giver": giver,
        "receiver": receiver,
    }


def withdrawal(scan_id, revision):
    return {"scan_id": scan_id, "revision": revision, "event_type": "WITHDRAWAL"}

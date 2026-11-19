"""End-to-end HTTP smoke test against a running uvicorn instance."""
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:" + os.environ.get("SMOKE_PORT", "8011")


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            return resp.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except json.JSONDecodeError:
            return e.code, payload


def check(label, cond, extra=""):
    print(("PASS" if cond else "FAIL"), label, extra)
    if not cond:
        sys.exit(1)


st, body = call("POST", "/api/waybills", {"waybill_id": "AWB-SMOKE", "origin_party": "ORIG"})
check("create waybill", st == 201, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z", "giver": "ORIG", "receiver": "B"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T09:00:00Z", "giver": "B", "receiver": "C"},
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "Y"},
]})
check("batch accepted", st == 200, st)
check("GAP detected", body["chain"]["break_reason"] == "GAP")
check("custodian stays at C", body["chain"]["current_custodian"] == "C")
check("S3 unverified", [s["scan_id"] for s in body["chain"]["unverified_scans"]] == ["S3"])

# same revision divergence -> 409, old chain preserved
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 1, "ts_utc": "2026-11-18T10:00:00Z", "giver": "X", "receiver": "TAMPERED"},
]})
check("same-revision divergence -> 409", st == 409, body)

st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 2, "event_type": "WITHDRAWAL"},
]})
check("withdrawal accepted", st == 200, body)
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S3", "revision": 3, "ts_utc": "2026-11-18T10:00:00Z", "giver": "C", "receiver": "D"},
]})
check("correction accepted", st == 200, st)
check("chain healed", body["chain"]["chain_intact"] is True)
check("custodian now D", body["chain"]["current_custodian"] == "D")

# simultaneous timestamps
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S4", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "D", "receiver": "E"},
    {"scan_id": "S5", "revision": 1, "ts_utc": "2026-11-18T11:00:00Z", "giver": "E", "receiver": "F"},
]})
check("simultaneous flagged", body["chain"]["break_reason"] == "SIMULTANEOUS")

st, body = call("GET", "/api/waybills/AWB-SMOKE/chain")
check("GET chain", st == 200 and body["waybill_id"] == "AWB-SMOKE")

st, body = call("GET", "/api/waybills")
check("list waybills", st == 200 and any(w["id"] == "AWB-SMOKE" for w in body["waybills"]))

st, body = call("GET", "/docs", raw=True)
check("swagger docs reachable", st == 200 and b"swagger" in body.lower())
st, body = call("GET", "/openapi.json")
check("openapi schema reachable", st == 200 and "paths" in body)
st, body = call("GET", "/", raw=True)
check("console page reachable", st == 200 and b"<!DOCTYPE html>" in body)

# route-prefix summary
st, body = call("GET", "/api/route-summary/status")
check("summary status ready", st == 200 and body["ready"] is True, body)

st, body = call("POST", "/api/route-summary", {"parties": ["ORIG", "B", "C"]})
check("route summary 200", st == 200 and body["ready"] is True, body)
# chain ORIG->B->C->D verified, then SIMULTANEOUS at E/F: trusted chain ends at D
groups = {g["next_party"]: g["waybill_count"] for g in body["next_custodians"]}
check("next custodian D counted once", groups == {"D": 1}, body)
check("no terminal at depth 3", body["no_further_scan_count"] == 0, body)

st, body = call("POST", "/api/route-summary", {"parties": ["ORIG", "B", "C", "D"]})
check("terminal at end of trusted chain", st == 200
      and body["matched_waybills"] == 1
      and body["no_further_scan_count"] == 1, body)

st, body = call("POST", "/api/route-summary", {"parties": ["ORIG", "B", "C", "D", "E"]})
check("route longer than chain does not match", st == 200
      and body["matched_waybills"] == 0, body)

st, body = call("POST", "/api/route-summary", {"parties": []})
check("empty route rejected", st == 422, body)
st, body = call("POST", "/api/route-summary", {"parties": ["ORIG", "  "]})
check("blank party rejected", st == 422, body)

print("ALL SMOKE CHECKS PASSED")

# validation: naive datetime
st, body = call("POST", "/api/waybills/AWB-SMOKE/scans/batch", {"scans": [
    {"scan_id": "S9", "revision": 1, "ts_utc": "2026-11-18T12:00:00", "giver": "D", "receiver": "E"},
]})
check("naive timestamp -> 422", st == 422)

print("ALL SMOKE CHECKS PASSED")

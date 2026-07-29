import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sim7080mini.httpclient import HttpClient
from sim7080mini.infrakey import InfrakeyClient
from sim7080mini.mock import SIM7080Modem


def _client():
    modem = SIM7080Modem(debug=0)
    return HttpClient(modem, host="api.infrakey.fasttrack.cloud", port=443, user_agent="test-agent")


def test_mock_health_override():
    os.environ["SIM7080_MOCK_HEALTH_STATUS"] = "503"
    client = _client()
    st, obj, dbg = client.get_json("/api/v1/health")
    assert st == 503
    return {"name": "mock_health_override", "status": "OK", "details": {"status": st}}


def test_mock_claim_override():
    os.environ["SIM7080_MOCK_CLAIM_STATUS"] = "500"
    client = _client()
    st, obj, dbg = client.post_json("/api/v1/devices/claim", {"imei": "1"})
    assert st == 500
    return {"name": "mock_claim_override", "status": "OK", "details": {"status": st}}


def test_mock_heartbeat_override():
    os.environ["SIM7080_MOCK_HEARTBEAT_STATUS"] = "201"
    os.environ["SIM7080_MOCK_NEXT_PULL_SEC"] = "45"
    client = _client()
    st, obj, dbg = client.post_json("/api/v1/devices/DEV/heartbeat", {"battery_v": 3.7})
    assert st == 201
    assert int(obj.get("next_pull_sec", 0)) == 45
    return {"name": "mock_heartbeat_override", "status": "OK", "details": obj}


def test_mock_ack_failure_override():
    os.environ["SIM7080_MOCK_ACK_STATUS"] = "500"
    client = _client()
    st, obj, dbg = client.post_json("/api/v1/devices/DEV/commands/CMD-1/ack", {"notes": "x"})
    assert st == 500
    return {"name": "mock_ack_failure_override", "status": "OK", "details": {"status": st}}


def test_infrakey_heartbeat_refresh_on_401():
    prev_cwd = os.getcwd()
    prev_mock = os.environ.get("SIM7080_USE_MOCK")
    tmpdir = tempfile.mkdtemp(prefix="ifk-auth-refresh-")
    try:
        os.chdir(tmpdir)
        os.environ["SIM7080_USE_MOCK"] = "1"

        cfg = {
            "host": "api.infrakey.fasttrack.cloud",
            "port": 443,
            "nb_band": 28,
            "apn_fallback": "m2m.mock.cl",
            "user_agent": "test-agent",
            "fw": "v1.0.0",
            "model": "SIM7080G-Pico",
            "latitude": -33.4489,
            "longitude": -70.6693,
            "http_retry_count": 2,
            "http_retry_backoff_ms": 1,
            "files": {
                "token": "token.json",
                "outbox": "outbox.jsonl",
                "outbox_state": "outbox_state.json",
            },
            "runtime": {
                "outbox_flush_max": 5,
                "offline_after_heartbeat_failures": 3,
            },
        }

        class ScriptedHttp:
            def __init__(self):
                self.calls = []
                self.heartbeat_calls = 0
                self.claim_calls = 0

            def post_json(self, path, body, headers=None):
                self.calls.append({"path": path, "headers": headers or {}, "body": body})
                if path == "/api/v1/devices/claim":
                    self.claim_calls += 1
                    return 201, {"device_id": "DEV-REFRESH", "auth_token": "TOK-REFRESH"}, ""
                if path.endswith("/heartbeat"):
                    self.heartbeat_calls += 1
                    if self.heartbeat_calls == 1:
                        return 401, {"error": "token_invalid"}, ""
                    return 201, {"ok": True, "next_pull_sec": 45}, ""
                return 201, {"ok": True}, ""

            def get_json(self, path, headers=None):
                return 200, {"ok": True}, ""

        client = InfrakeyClient(cfg, debug=0)
        client.http = ScriptedHttp()
        client._save_token("DEV-OLD", "TOK-OLD")
        client.device_id = "DEV-OLD"
        client.auth_token = "TOK-OLD"

        st, obj, dbg = client.heartbeat("DEV-OLD", "TOK-OLD", battery_v=3.7)
        assert st == 201
        assert int(obj.get("next_pull_sec", 0)) == 45
        assert client.device_id == "DEV-REFRESH"
        assert client.auth_token == "TOK-REFRESH"
        assert client.http.claim_calls == 1
        assert client.http.heartbeat_calls == 2
        return {
            "name": "infrakey_heartbeat_refresh_on_401",
            "status": "OK",
            "details": {
                "device_id": client.device_id,
                "claim_calls": client.http.claim_calls,
                "heartbeat_calls": client.http.heartbeat_calls,
            },
        }
    finally:
        os.chdir(prev_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)
        if prev_mock is None:
            os.environ.pop("SIM7080_USE_MOCK", None)
        else:
            os.environ["SIM7080_USE_MOCK"] = prev_mock


def test_claim_and_heartbeat_include_location():
    prev_cwd = os.getcwd()
    prev_mock = os.environ.get("SIM7080_USE_MOCK")
    tmpdir = tempfile.mkdtemp(prefix="ifk-location-contract-")
    try:
        os.chdir(tmpdir)
        os.environ["SIM7080_USE_MOCK"] = "1"
        latitude = -41.46294
        longitude = -72.96671
        cfg = {
            "host": "api.infrakey.fasttrack.cloud",
            "port": 443,
            "nb_band": 28,
            "apn_fallback": "m2m.mock.cl",
            "user_agent": "test-agent",
            "fw": "v1.0.0",
            "model": "Raspberry-Pi-G4",
            "latitude": latitude,
            "longitude": longitude,
            "gps": {
                "mode": "static_config",
                "allow_static": True,
                "include_source": True,
                "cache_ms": 900000,
            },
            "http_retry_count": 1,
            "http_retry_backoff_ms": 0,
            "files": {
                "token": "token.json",
                "outbox": "outbox.jsonl",
                "outbox_state": "outbox_state.json",
            },
            "runtime": {
                "outbox_flush_max": 5,
                "offline_after_heartbeat_failures": 3,
            },
        }

        class CaptureHttp:
            def __init__(self):
                self.calls = []

            def post_json(self, path, body, headers=None):
                self.calls.append({"path": path, "body": dict(body)})
                if path == "/api/v1/devices/claim":
                    return 201, {"device_id": "DEV-LOC", "auth_token": "TOK-LOC"}, ""
                return 200, {"ok": True, "next_pull_sec": 900}, ""

            def get_json(self, path, headers=None):
                return 200, {"ok": True}, ""

        client = InfrakeyClient(cfg, debug=0)
        client.http = CaptureHttp()
        device_id, token = client.claim_if_needed()
        assert device_id == "DEV-LOC"
        assert token == "TOK-LOC"
        st, _, _ = client.heartbeat(device_id, token)
        assert st == 200

        claim_body = client.http.calls[0]["body"]
        heartbeat_body = client.http.calls[1]["body"]
        for body in (claim_body, heartbeat_body):
            assert body["latitude"] == latitude
            assert body["longitude"] == longitude
        return {
            "name": "claim_and_heartbeat_include_location",
            "status": "OK",
            "details": {
                "claim": {
                    "latitude": claim_body["latitude"],
                    "longitude": claim_body["longitude"],
                },
                "heartbeat": {
                    "latitude": heartbeat_body["latitude"],
                    "longitude": heartbeat_body["longitude"],
                },
            },
        }
    finally:
        os.chdir(prev_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)
        if prev_mock is None:
            os.environ.pop("SIM7080_USE_MOCK", None)
        else:
            os.environ["SIM7080_USE_MOCK"] = prev_mock


def run_all():
    keys = [
        "SIM7080_MOCK_HEALTH_STATUS",
        "SIM7080_MOCK_CLAIM_STATUS",
        "SIM7080_MOCK_HEARTBEAT_STATUS",
        "SIM7080_MOCK_NEXT_PULL_SEC",
        "SIM7080_MOCK_ACK_STATUS",
    ]
    prev_env = {k: os.environ.get(k) for k in keys}
    try:
        tests = [
            test_mock_health_override,
            test_mock_claim_override,
            test_mock_heartbeat_override,
            test_mock_ack_failure_override,
            test_infrakey_heartbeat_refresh_on_401,
            test_claim_and_heartbeat_include_location,
        ]
        results = []
        for test_fn in tests:
            try:
                for key in keys:
                    os.environ.pop(key, None)
                results.append(test_fn())
            except Exception as exc:
                results.append({"name": test_fn.__name__, "status": "FAIL", "details": str(exc)})
        return results
    finally:
        for key, value in prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    results = run_all()
    summary = {
        "ok": sum(1 for r in results if r["status"] == "OK"),
        "fail": sum(1 for r in results if r["status"] != "OK"),
        "results": results,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))

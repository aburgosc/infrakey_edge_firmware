import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["SIM7080_USE_MOCK"] = "1"

from sim7080mini.actuator import Actuator
from sim7080mini.config import load_config
from sim7080mini.handlers import handle_command, handle_periodic_tasks
from sim7080mini.hal import make_hal
from sim7080mini.outbox import JsonlEventOutbox
from sim7080mini.ws_feeder import WebSocketCommandFeeder


@contextmanager
def _pushd(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


class SimpleCmd:
    def __init__(self, cmd_id, cmd_type, payload=None, source="test"):
        self.id = cmd_id
        self.type = cmd_type
        self.payload = payload or {}
        self.source = source


class WsAckStub:
    def __init__(self):
        self.acks = []

    def can_ack(self):
        return True

    def send_ack(self, cmd_id, ok, notes=""):
        self.acks.append({"id": cmd_id, "ok": bool(ok), "notes": notes})
        return True


class MockAppClient:
    def __init__(self, cfg, debug=0):
        self.cfg = cfg
        self.debug = debug
        self.hal = make_hal(debug=0)
        self.fw = cfg["fw"]
        self.latitude = cfg["latitude"]
        self.longitude = cfg["longitude"]
        self.runtime_state = {
            "heartbeat_failures": 0,
            "device_offline_queued": False,
            "battery_low_active": False,
            "last_heartbeat_status": None,
            "last_heartbeat_ok": None,
            "last_heartbeat_next_pull_sec": None,
        }
        self.events = []
        self.acks = []
        self.snapshots = []
        self.heartbeats = []
        self.outbox = JsonlEventOutbox(
            outbox_path=cfg["files"]["outbox"],
            state_path=cfg["files"].get("outbox_state", "outbox_state.json"),
            debug=0,
        )

    def ack_command(self, device_id, auth_token, cmd_id, ack_at=None, notes=""):
        self.acks.append({"id": cmd_id, "ack_at": ack_at, "notes": notes})
        return 201, {"ok": True}, ""

    def send_snapshot(self, device_id, auth_token, snapshot_dict):
        self.snapshots.append(snapshot_dict)
        return 201, {"ok": True, "snapshot": True}, ""

    def report_event(self, device_id, auth_token, status, severity, event_id, ts=None, extra=None, queue_on_fail=False):
        event = {
            "status": status,
            "severity": severity,
            "event_id": event_id,
            "ts": ts,
            "extra": extra,
            "queue_on_fail": queue_on_fail,
        }
        self.events.append(event)
        if queue_on_fail and status in ("battery_low", "device_offline"):
            self.queue_event(status=status, severity=severity, event_id=event_id, ts=ts, extra=extra)
        return 201, {"ok": True, "stored": True}, ""

    def heartbeat(self, device_id, auth_token, battery_v=None, battery_pct=None, latitude=None, longitude=None, extra=None):
        self.heartbeats.append({
            "device_id": device_id,
            "auth_token": auth_token,
            "battery_v": battery_v,
            "battery_pct": battery_pct,
            "latitude": latitude,
            "longitude": longitude,
            "extra": extra or {},
        })
        return 201, {"ok": True, "next_pull_sec": 120}, ""

    def queue_event(self, status, severity, event_id, ts=None, extra=None):
        body = {"status": status, "severity": severity, "event_id": event_id}
        if ts is not None:
            body["ts"] = ts
        if extra:
            body["extra"] = extra
        return self.outbox.enqueue(body)

    def flush_event_outbox(self, device_id, auth_token, max_n=5):
        accepted = []

        def sender(record):
            accepted.append(record["event_id"])
            return True

        flushed = self.outbox.flush(sender, max_n=max_n)
        self.outbox.compact_if_needed(min_bytes=16, min_ratio_pct=10)
        return flushed

    def note_heartbeat_status(self, status, flush_device_id=None, flush_token=None):
        self.runtime_state["last_heartbeat_status"] = status
        self.runtime_state["last_heartbeat_ok"] = bool(status in (200, 201))
        if status in (200, 201):
            self.runtime_state["heartbeat_failures"] = 0
            self.runtime_state["device_offline_queued"] = False
            if flush_device_id and flush_token:
                self.flush_event_outbox(flush_device_id, flush_token, max_n=10)
            return
        self.runtime_state["heartbeat_failures"] += 1
        if self.runtime_state["heartbeat_failures"] >= int(self.cfg["runtime"]["offline_after_heartbeat_failures"]):
            if not self.runtime_state["device_offline_queued"]:
                self.runtime_state["device_offline_queued"] = True
                self.queue_event(
                    status="device_offline",
                    severity="warning",
                    event_id="offline-{}".format(self.hal.ticks_ms()),
                    ts=self.hal.now_utc_iso(),
                    extra={"reason": "heartbeat_failures", "count": self.runtime_state["heartbeat_failures"]},
                )


def _make_cfg():
    cfg = load_config()
    cfg["debug"] = 0
    cfg["ws_enabled"] = False
    cfg["runtime"]["offline_after_heartbeat_failures"] = 2
    cfg["runtime"]["outbox_flush_max"] = 10
    cfg["features"]["allow_snapshot"] = True
    cfg["battery"]["adc_pin"] = None
    cfg["gpio"]["mode"] = "servo"
    cfg["gpio"]["servo_pwm_pin"] = 20
    cfg["gpio"]["sensor_pin"] = 15
    cfg["gpio"]["sensor_open_is"] = 1
    cfg["gpio"]["sensor_pull"] = "down"
    cfg["gpio"]["sensor_debounce_ms"] = 0
    return cfg


def test_config_snapshot_and_ping(tmpdir):
    with _pushd(tmpdir):
        os.environ["SIM7080_MOCK_BATTERY_V"] = "3.70"
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()

        res_cfg = handle_command(
            SimpleCmd("cfg-1", "update_config", {"heartbeat_interval": 120, "low_battery_threshold": 3.4, "allow_snapshot": True}),
            client, actuator, cfg, "DEV", "TOK", ws=ws,
        )
        res_ping = handle_command(SimpleCmd("ping-1", "ping"), client, actuator, cfg, "DEV", "TOK", ws=ws)
        res_snap = handle_command(SimpleCmd("snap-1", "snapshot"), client, actuator, cfg, "DEV", "TOK", ws=ws)

        assert res_cfg["local_ok"] is True
        assert res_ping["local_ok"] is True
        assert res_snap["local_ok"] is True
        assert "pong" in res_ping["notes"]
        assert "uptime=" in res_ping["notes"]
        assert cfg["heartbeat_interval_sec"] == 120
        assert float(cfg["thresholds"]["low_battery_v"]) == 3.4
        assert len(client.snapshots) == 1
        return {"name": "config_snapshot_and_ping", "status": "OK", "details": {"acks": len(ws.acks), "ping_notes": res_ping["notes"]}}


def test_authorization_and_command_flow(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()

        res_open = handle_command(SimpleCmd("open-1", "open_actuator"), client, actuator, cfg, "DEV", "TOK", ws=ws)
        res_pulse = handle_command(SimpleCmd("pulse-1", "pulse_actuator", {"duration_ms": 250}), client, actuator, cfg, "DEV", "TOK", ws=ws)
        res_close = handle_command(SimpleCmd("close-1", "close_actuator"), client, actuator, cfg, "DEV", "TOK", ws=ws)

        statuses = [e["status"] for e in client.events]
        assert res_open["local_ok"] is True
        assert res_pulse["local_ok"] is True
        assert res_close["local_ok"] is True
        assert "authorization_request" in statuses
        assert "authorization_granted" in statuses
        assert "device_opened" in statuses
        assert "device_closed" in statuses
        return {"name": "authorization_and_command_flow", "status": "OK", "details": {"events": statuses}}


def test_periodic_battery_low_and_offline_outbox(tmpdir):
    with _pushd(tmpdir):
        os.environ["SIM7080_MOCK_BATTERY_V"] = "3.20"
        cfg = _make_cfg()
        cfg["thresholds"]["low_battery_v"] = 3.3
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        client.runtime_state["heartbeat_failures"] = 0

        _, next_sleep = handle_periodic_tasks(client, actuator, "DEV", "TOK", 0, 1)
        statuses = [e["status"] for e in client.events]
        assert "battery_low" in statuses
        assert client.runtime_state["battery_low_active"] is True
        client.note_heartbeat_status(503)
        client.note_heartbeat_status(503)
        outbox_content = open(cfg["files"]["outbox"], "r", encoding="utf-8").read()
        assert "device_offline" in outbox_content
        assert client.runtime_state["device_offline_queued"] is True
        assert int(next_sleep) == 120
        return {
            "name": "periodic_battery_low_and_offline_outbox",
            "status": "OK",
            "details": {"next_sleep": next_sleep, "events": statuses, "outbox_len": len(outbox_content)},
        }


def test_temporizer_clamp(tmpdir):
    with _pushd(tmpdir):
        os.environ["SIM7080_MOCK_BATTERY_V"] = "3.60"
        cfg = _make_cfg()
        cfg["next_pull_min_sec"] = 30
        cfg["next_pull_max_sec"] = 300
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])

        _, next_sleep = handle_periodic_tasks(client, actuator, "DEV", "TOK", 0, 1)
        assert 30 <= int(next_sleep) <= 300
        return {"name": "temporizer_clamp", "status": "OK", "details": {"next_sleep": next_sleep}}


def test_snapshot_disabled(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        cfg["features"]["allow_snapshot"] = False
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()
        result = handle_command(SimpleCmd("snap-off", "snapshot"), client, actuator, cfg, "DEV", "TOK", ws=ws)
        assert result["local_ok"] is False
        assert len(client.snapshots) == 0
        return {"name": "snapshot_disabled", "status": "OK", "details": result}


def test_snapshot_alias_instantanea(tmpdir):
    with _pushd(tmpdir):
        os.environ["SIM7080_MOCK_BATTERY_V"] = "3.80"
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()
        result = handle_command(SimpleCmd("snap-alias", "instantanea"), client, actuator, cfg, "DEV", "TOK", ws=ws)
        assert result["local_ok"] is True
        assert len(client.snapshots) == 1
        return {"name": "snapshot_alias_instantanea", "status": "OK", "details": result}


def test_power_profile_update(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()
        result = handle_command(
            SimpleCmd("pp-1", "update_config", {"power_profile": "low_power"}),
            client,
            actuator,
            cfg,
            "DEV",
            "TOK",
            ws=ws,
        )
        assert result["local_ok"] is True
        assert cfg["ws_enabled"] is False
        assert int(cfg["heartbeat_interval_sec"]) >= 1800
        assert cfg["runtime"]["power_profile"] == "low_power"
        return {"name": "power_profile_update", "status": "OK", "details": {"heartbeat_interval_sec": cfg["heartbeat_interval_sec"]}}


def test_telemetry_fallback_and_static_gps(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()
        os.environ["SIM7080_MOCK_BATTERY_V"] = ""
        result = handle_command(SimpleCmd("snap-gps", "snapshot"), client, actuator, cfg, "DEV", "TOK", ws=ws)
        assert result["local_ok"] is True
        snap = client.snapshots[0]
        assert snap.get("gps_source") == "static_config"
        assert "telemetry_missing" in snap
        assert "battery_v" not in snap
        return {"name": "telemetry_fallback_and_static_gps", "status": "OK", "details": snap}


def test_update_config_validation_guards(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()

        bad_hb = handle_command(SimpleCmd("cfg-bad-hb", "update_config", {"heartbeat_interval": "abc"}), client, actuator, cfg, "DEV", "TOK", ws=ws)
        bad_batt = handle_command(SimpleCmd("cfg-bad-batt", "update_config", {"low_battery_threshold": 5.5}), client, actuator, cfg, "DEV", "TOK", ws=ws)
        bad_gpio = handle_command(SimpleCmd("cfg-bad-gpio", "update_config", {"gpio": {"mode": "boom"}}), client, actuator, cfg, "DEV", "TOK", ws=ws)
        good_profile = handle_command(
            SimpleCmd("cfg-good-profile", "update_config", {"power_profile": {"heartbeat_interval": 600, "ws_enabled": True, "persist_ws_commands": False}}),
            client,
            actuator,
            cfg,
            "DEV",
            "TOK",
            ws=ws,
        )

        assert bad_hb["local_ok"] is False
        assert "heartbeat_interval_invalid" in bad_hb["notes"]
        assert bad_batt["local_ok"] is False
        assert "low_battery_threshold_invalid" in bad_batt["notes"]
        assert bad_gpio["local_ok"] is False
        assert "gpio_mode_invalid" in bad_gpio["notes"]
        assert good_profile["local_ok"] is True
        assert int(cfg["heartbeat_interval_sec"]) == 600
        assert cfg["ws_enabled"] is True
        return {
            "name": "update_config_validation_guards",
            "status": "OK",
            "details": {
                "bad_hb": bad_hb["notes"],
                "bad_batt": bad_batt["notes"],
                "bad_gpio": bad_gpio["notes"],
                "profile": good_profile["notes"],
            },
        }


def test_heartbeat_failure_recovery_state(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])

        client.note_heartbeat_status(503)
        client.note_heartbeat_status(503)
        assert client.runtime_state["heartbeat_failures"] == 2
        assert client.runtime_state["device_offline_queued"] is True

        client.note_heartbeat_status(201, flush_device_id="DEV", flush_token="TOK")
        assert client.runtime_state["heartbeat_failures"] == 0
        assert client.runtime_state["device_offline_queued"] is False
        assert client.runtime_state["last_heartbeat_status"] == 201
        assert client.runtime_state["last_heartbeat_ok"] is True
        return {
            "name": "heartbeat_failure_recovery_state",
            "status": "OK",
            "details": {
                "last_status": client.runtime_state["last_heartbeat_status"],
                "failures": client.runtime_state["heartbeat_failures"],
                "offline_queued": client.runtime_state["device_offline_queued"],
            },
        }


def test_next_pull_invalid_clamp(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        cfg["next_pull_min_sec"] = 30
        cfg["next_pull_max_sec"] = 300
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])

        def invalid_heartbeat(device_id, auth_token, battery_v=None, battery_pct=None, latitude=None, longitude=None, extra=None):
            client.heartbeats.append({
                "device_id": device_id,
                "auth_token": auth_token,
                "battery_v": battery_v,
                "battery_pct": battery_pct,
                "latitude": latitude,
                "longitude": longitude,
                "extra": extra or {},
            })
            return 201, {"ok": True, "next_pull_sec": 999999}, ""

        client.heartbeat = invalid_heartbeat
        _, next_sleep = handle_periodic_tasks(client, actuator, "DEV", "TOK", 0, 1)
        assert int(next_sleep) == 300
        return {"name": "next_pull_invalid_clamp", "status": "OK", "details": {"next_sleep": next_sleep}}


def test_periodic_heartbeat_controlled_telemetry(tmpdir):
    with _pushd(tmpdir):
        os.environ["SIM7080_MOCK_BATTERY_V"] = "3.96"
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])

        last_hb_ms, next_sleep = handle_periodic_tasks(client, actuator, "DEV", "TOK", 0, 1)
        assert len(client.heartbeats) == 1
        hb = client.heartbeats[0]
        assert round(float(hb["battery_v"]), 2) == 3.96
        assert int(hb["battery_pct"]) >= 70
        assert hb["latitude"] == cfg["latitude"]
        assert hb["longitude"] == cfg["longitude"]
        assert hb["extra"].get("gps_source") == "static_config"
        assert "telemetry_missing" not in hb["extra"]
        assert client.runtime_state["last_heartbeat_status"] == 201
        assert client.runtime_state["last_heartbeat_next_pull_sec"] == 120
        return {
            "name": "periodic_heartbeat_controlled_telemetry",
            "status": "OK",
            "details": hb,
        }


def test_local_action_survives_event_report_failure(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()

        def failing_report_event(device_id, auth_token, status, severity, event_id, ts=None, extra=None, queue_on_fail=False):
            client.events.append({
                "status": status,
                "severity": severity,
                "event_id": event_id,
                "ts": ts,
                "extra": extra,
                "queue_on_fail": queue_on_fail,
            })
            return 500, {"error": "event_failed"}, ""

        client.report_event = failing_report_event
        result = handle_command(SimpleCmd("open-fail-report", "open_actuator"), client, actuator, cfg, "DEV", "TOK", ws=ws)
        assert result["local_ok"] is True
        assert result["ack_http_ok"] is True
        assert "report:fail:500" in result["notes"]
        assert "auth_request:fail:500" in result["notes"] or "auth_request:ok" in result["notes"]
        return {"name": "local_action_survives_event_report_failure", "status": "OK", "details": result}


def test_ws_queue_overflow_tracking(tmpdir):
    with _pushd(tmpdir):
        hal = make_hal(debug=0)
        feeder = WebSocketCommandFeeder(
            modem=hal.m,
            hal=hal,
            host="api.infrakey.fasttrack.cloud",
            token="TOK",
            identifier_extra={"device_id": "DEV"},
            debug=0,
            max_queue=2,
        )
        assert feeder._enqueue_command(SimpleCmd("w1", "ping")) is True
        assert feeder._enqueue_command(SimpleCmd("w2", "ping")) is True
        assert feeder._enqueue_command(SimpleCmd("w3", "ping")) is False
        stats = feeder.stats()
        assert stats["queued"] == 2
        assert stats["dropped"] == 1
        assert stats["max_queue"] == 2
        return {"name": "ws_queue_overflow_tracking", "status": "OK", "details": stats}


def test_command_payload_validation_matrix(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()

        invalid_ping = handle_command(SimpleCmd("bad-ping", "ping", payload=[1]), client, actuator, cfg, "DEV", "TOK", ws=ws)
        invalid_pulse = handle_command(SimpleCmd("bad-pulse", "pulse_actuator", {"duration_ms": -10}), client, actuator, cfg, "DEV", "TOK", ws=ws)
        invalid_event = handle_command(SimpleCmd("bad-event", "test_event", {"status": "tamper_alert", "severity": "bogus"}), client, actuator, cfg, "DEV", "TOK", ws=ws)
        invalid_cfg = handle_command(SimpleCmd("bad-cfg", "update_config", {"runtime": {"ws_enabled": False}}), client, actuator, cfg, "DEV", "TOK", ws=ws)

        assert invalid_ping["local_ok"] is False
        assert "payload_invalid_type" in invalid_ping["notes"]
        assert invalid_pulse["local_ok"] is False
        assert "pulse_invalid_duration" in invalid_pulse["notes"]
        assert invalid_event["local_ok"] is False
        assert "event_severity_invalid" in invalid_event["notes"]
        assert invalid_cfg["local_ok"] is False
        assert "config_key_unsupported:runtime" in invalid_cfg["notes"]
        return {
            "name": "command_payload_validation_matrix",
            "status": "OK",
            "details": {
                "ping": invalid_ping["notes"],
                "pulse": invalid_pulse["notes"],
                "event": invalid_event["notes"],
                "config": invalid_cfg["notes"],
            },
        }


def test_tamper_alert_dedicated_and_suppressed(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        cfg["runtime"]["tamper_repeat_suppress_ms"] = 60000
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        actuator.tamper_triggered = lambda: True

        handle_periodic_tasks(client, actuator, "DEV", "TOK", client.hal.ticks_ms(), 9999)
        handle_periodic_tasks(client, actuator, "DEV", "TOK", client.hal.ticks_ms(), 9999)

        statuses = [e["status"] for e in client.events]
        assert statuses.count("tamper_alert") == 1
        assert statuses.count("unauthorized_access") == 1
        tamper = next(e for e in client.events if e["status"] == "tamper_alert")
        unauthorized = next(e for e in client.events if e["status"] == "unauthorized_access")
        assert tamper["severity"] == "warning"
        assert unauthorized["severity"] == "emergency"
        return {
            "name": "tamper_alert_dedicated_and_suppressed",
            "status": "OK",
            "details": {
                "events": statuses,
                "last_tamper_event_ms": client.runtime_state.get("last_tamper_event_ms"),
            },
        }


def test_event_delivery_priority_policy(tmpdir):
    with _pushd(tmpdir):
        cfg = _make_cfg()
        client = MockAppClient(cfg, debug=0)
        actuator = Actuator(client.hal, cfg["gpio"])
        ws = WsAckStub()

        normal = handle_command(
            SimpleCmd("evt-normal", "test_event", {"status": "device_closed", "severity": "info"}),
            client,
            actuator,
            cfg,
            "DEV",
            "TOK",
            ws=ws,
        )
        critical = handle_command(
            SimpleCmd("evt-critical", "test_event", {"status": "unauthorized_access", "severity": "emergency"}),
            client,
            actuator,
            cfg,
            "DEV",
            "TOK",
            ws=ws,
        )

        assert normal["local_ok"] is True
        assert critical["local_ok"] is True
        normal_evt = next(e for e in client.events if e["event_id"] == "evt-normal")
        critical_evt = next(e for e in client.events if e["event_id"] == "evt-critical")
        assert normal_evt["queue_on_fail"] is False
        assert critical_evt["queue_on_fail"] is True
        return {
            "name": "event_delivery_priority_policy",
            "status": "OK",
            "details": {
                "normal_queue_on_fail": normal_evt["queue_on_fail"],
                "critical_queue_on_fail": critical_evt["queue_on_fail"],
            },
        }


def test_ws_health_timeouts(tmpdir):
    with _pushd(tmpdir):
        hal = make_hal(debug=0)
        feeder = WebSocketCommandFeeder(
            modem=hal.m,
            hal=hal,
            host="api.infrakey.fasttrack.cloud",
            token="TOK",
            identifier_extra={"device_id": "DEV"},
            debug=0,
            max_queue=2,
        )

        now = hal.ticks_ms()

        feeder.connected = True
        feeder.subscribed = False
        feeder._connected_at_ms = now - 50
        assert feeder.is_healthy(idle_timeout_ms=1000, confirm_timeout_ms=10) is False

        feeder.connected = True
        feeder.subscribed = True
        feeder._last_rx_ms = now - 50
        assert feeder.is_healthy(idle_timeout_ms=10, confirm_timeout_ms=1000) is False

        feeder.connected = True
        feeder.subscribed = True
        feeder._last_rx_ms = hal.ticks_ms()
        assert feeder.is_healthy(idle_timeout_ms=1000, confirm_timeout_ms=1000) is True

        return {
            "name": "ws_health_timeouts",
            "status": "OK",
            "details": {
                "confirm_timeout_unhealthy": True,
                "idle_timeout_unhealthy": True,
                "recent_rx_healthy": True,
            },
        }


def run_all():
    tmpdir = tempfile.mkdtemp(prefix="ifk-mock-flow-")
    try:
        tests = [
            test_config_snapshot_and_ping,
            test_authorization_and_command_flow,
            test_periodic_battery_low_and_offline_outbox,
            test_temporizer_clamp,
            test_snapshot_disabled,
            test_snapshot_alias_instantanea,
            test_power_profile_update,
            test_telemetry_fallback_and_static_gps,
            test_update_config_validation_guards,
            test_heartbeat_failure_recovery_state,
            test_next_pull_invalid_clamp,
            test_periodic_heartbeat_controlled_telemetry,
            test_local_action_survives_event_report_failure,
            test_ws_queue_overflow_tracking,
            test_command_payload_validation_matrix,
            test_tamper_alert_dedicated_and_suppressed,
            test_event_delivery_priority_policy,
            test_ws_health_timeouts,
        ]
        results = []
        for test_fn in tests:
            try:
                results.append(test_fn(tmpdir))
            except Exception as exc:
                results.append({"name": test_fn.__name__, "status": "FAIL", "details": str(exc)})
        return results
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    results = run_all()
    summary = {
        "ok": sum(1 for r in results if r["status"] == "OK"),
        "fail": sum(1 for r in results if r["status"] != "OK"),
        "results": results,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))

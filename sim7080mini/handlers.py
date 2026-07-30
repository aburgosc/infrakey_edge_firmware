from sim7080mini.config import save_config

try:
    import utime as _time
except Exception:
    import time as _time


def _safe_int(v, default=None):
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


def _safe_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
    return None


def _clamp(v, min_v, max_v):
    if v < min_v:
        return min_v
    if v > max_v:
        return max_v
    return v


def _local_heartbeat_sec(cfg, value=None, fallback=300):
    min_sec = _safe_int(cfg.get("heartbeat_interval_min_sec", 60), 60)
    max_sec = _safe_int(cfg.get("heartbeat_interval_max_sec", 86400), 86400)
    hb = _safe_int(cfg.get("heartbeat_interval_sec", fallback) if value is None else value, fallback)
    return _clamp(hb, min_sec, max_sec)


def now_iso_utc_or_none(hal):
    try:
        if hasattr(hal, "now_utc_iso"):
            return hal.now_utc_iso()
    except Exception:
        pass
    return None


def _read_battery_metrics(client):
    cfg_bat = client.cfg.get("battery", {})
    voltage = None
    percent = None
    try:
        voltage = client.hal.read_battery_voltage(
            adc_pin=cfg_bat.get("adc_pin"),
            divider_ratio=cfg_bat.get("divider_ratio", 2.0),
            vref=cfg_bat.get("vref", 3.3),
            samples=cfg_bat.get("samples", 4),
        )
    except Exception:
        voltage = None
    if voltage is not None:
        try:
            percent = client.hal.battery_percent(
                voltage,
                empty_v=cfg_bat.get("empty_v", 3.3),
                full_v=cfg_bat.get("full_v", 4.2),
            )
        except Exception:
            percent = None
    return voltage, percent


def _base_telemetry(client):
    battery_v, battery_pct = _read_battery_metrics(client)
    gps_cfg = client.cfg.get("gps", {})
    telemetry_missing = []
    payload = {
        "lte": "OK",
        "fw": client.fw,
    }
    gps_mode = gps_cfg.get("mode", "static_config")
    allow_static = bool(gps_cfg.get("allow_static", True))
    location = {}
    try:
        location = client.resolve_location(force=False)
    except Exception:
        location = {}
    if "latitude" in location:
        payload["latitude"] = location["latitude"]
    if "longitude" in location:
        payload["longitude"] = location["longitude"]
    if "gps_source" in location:
        payload["gps_source"] = location["gps_source"]
    if "latitude" not in payload or "longitude" not in payload:
        telemetry_missing.append("gps")
        if gps_mode == "static_config" and allow_static and gps_cfg.get("include_source", True):
            payload["gps_source"] = "static_config"
    if battery_v is not None:
        payload["battery_v"] = round(float(battery_v), 3)
    else:
        telemetry_missing.append("battery_v")
    if battery_pct is not None:
        payload["battery_pct"] = int(battery_pct)
    else:
        telemetry_missing.append("battery_pct")
    if telemetry_missing:
        payload["telemetry_missing"] = telemetry_missing
    return payload


def _event_delivery_policy(status, severity):
    immediate_statuses = {
        "tamper_alert",
        "unauthorized_access",
        "battery_low",
        "device_offline",
        "open_failed",
        "close_failed",
        "pulse_failed",
    }
    if status in immediate_statuses:
        return True
    return severity in ("critical", "emergency")


def _validate_gpio_patch(patch):
    if not isinstance(patch, dict):
        return False, "gpio_invalid_type"
    allowed = {
        "mode", "actuator_pin", "actuator_active_high", "actuator_pulse_ms",
        "open_pin", "close_pin", "servo_pwm_pin", "servo_freq",
        "servo_open_us", "servo_close_us", "servo_drive_ms",
        "sensor_pin", "sensor_open_is", "sensor_pull", "sensor_debounce_ms",
        "sensor_authorized_open_ms", "sensor_alert_if_open_on_boot",
        "sensor_boot_grace_ms",
        "tamper_pin", "tamper_pull", "tamper_active_high",
    }
    out = {}
    for key, value in patch.items():
        if key not in allowed:
            return False, "gpio_key_unsupported:{}".format(key)
        if key == "mode":
            if value not in ("relay", "servo"):
                return False, "gpio_mode_invalid"
            out[key] = value
        elif key in ("sensor_pull", "tamper_pull"):
            if value not in ("up", "down", None):
                return False, "gpio_pull_invalid:{}".format(key)
            out[key] = value
        elif key in (
            "actuator_active_high",
            "tamper_active_high",
            "sensor_alert_if_open_on_boot",
        ):
            b = _safe_bool(value)
            if b is None:
                return False, "gpio_bool_invalid:{}".format(key)
            out[key] = b
        elif key == "sensor_open_is":
            iv = _safe_int(value, None)
            if iv not in (0, 1):
                return False, "gpio_sensor_open_invalid"
            out[key] = iv
        elif key in (
            "sensor_debounce_ms",
            "sensor_authorized_open_ms",
            "sensor_boot_grace_ms",
        ):
            iv = _safe_int(value, None)
            max_value = 2000 if key == "sensor_debounce_ms" else 60000
            if iv is None or iv < 0 or iv > max_value:
                return False, "gpio_range_invalid:{}".format(key)
            out[key] = iv
        else:
            iv = _safe_int(value, None)
            if iv is None or iv < 0:
                return False, "gpio_int_invalid:{}".format(key)
            out[key] = iv
    return True, out


def _apply_update_config(cfg: dict, payload: dict):
    if not isinstance(payload, dict) or not payload:
        return False, "empty_payload"

    changed = False
    allowed_top = {"heartbeat_interval", "low_battery_threshold", "allow_snapshot", "gpio", "power_profile"}
    for key in payload.keys():
        if key not in allowed_top:
            return False, "config_key_unsupported:{}".format(key)

    if "heartbeat_interval" in payload:
        hb = _safe_int(payload["heartbeat_interval"], None)
        if hb is None:
            return False, "heartbeat_interval_invalid"
        cfg["heartbeat_interval_sec"] = _local_heartbeat_sec(cfg, value=hb, fallback=300)
        changed = True

    if "low_battery_threshold" in payload:
        low = _safe_float(payload["low_battery_threshold"], None)
        if low is None or low < 2.5 or low > 4.5:
            return False, "low_battery_threshold_invalid"
        cfg.setdefault("thresholds", {})
        cfg["thresholds"]["low_battery_v"] = low
        changed = True

    if "allow_snapshot" in payload:
        allow = _safe_bool(payload["allow_snapshot"])
        if allow is None:
            return False, "allow_snapshot_invalid"
        cfg.setdefault("features", {})
        cfg["features"]["allow_snapshot"] = allow
        changed = True

    if "gpio" in payload:
        ok_gpio, gpio_patch = _validate_gpio_patch(payload["gpio"])
        if not ok_gpio:
            return False, gpio_patch
        cfg.setdefault("gpio", {})
        cfg["gpio"].update(gpio_patch)
        changed = True

    if "power_profile" in payload:
        profile = payload.get("power_profile")
        runtime = cfg.setdefault("runtime", {})
        if isinstance(profile, str):
            profile_name = profile.strip().lower()
            if profile_name == "low_power":
                cfg["heartbeat_interval_sec"] = _local_heartbeat_sec(cfg, value=max(1800, _local_heartbeat_sec(cfg)))
                cfg["ws_enabled"] = False
                runtime["persist_ws_commands"] = False
                runtime["legacy_import_batch"] = 0
            elif profile_name == "balanced":
                cfg["heartbeat_interval_sec"] = _local_heartbeat_sec(cfg, value=900)
                cfg["ws_enabled"] = True
                runtime["ws_reconnect_delay_ms"] = 5000
                runtime["ws_idle_timeout_ms"] = 90000
                runtime["persist_ws_commands"] = False
            elif profile_name == "realtime":
                cfg["heartbeat_interval_sec"] = _local_heartbeat_sec(cfg, value=300)
                cfg["ws_enabled"] = True
                runtime["ws_reconnect_delay_ms"] = 3000
                runtime["ws_idle_timeout_ms"] = 60000
                runtime["persist_ws_commands"] = False
            else:
                return False, "power_profile_invalid"
            runtime["power_profile"] = profile_name
            changed = True
        elif isinstance(profile, dict):
            hb = profile.get("heartbeat_interval")
            if hb is not None:
                hb = _safe_int(hb, None)
                if hb is None:
                    return False, "power_profile_heartbeat_invalid"
                cfg["heartbeat_interval_sec"] = _local_heartbeat_sec(cfg, value=hb)
            if "ws_enabled" in profile:
                ws_enabled = _safe_bool(profile.get("ws_enabled"))
                if ws_enabled is None:
                    return False, "power_profile_ws_enabled_invalid"
                cfg["ws_enabled"] = ws_enabled
            if "ws_reconnect_delay_ms" in profile:
                delay = _safe_int(profile.get("ws_reconnect_delay_ms"), None)
                if delay is None or delay < 1000 or delay > 600000:
                    return False, "power_profile_ws_reconnect_invalid"
                runtime["ws_reconnect_delay_ms"] = delay
            if "ws_idle_timeout_ms" in profile:
                timeout = _safe_int(profile.get("ws_idle_timeout_ms"), None)
                if timeout is None or timeout < 5000 or timeout > 600000:
                    return False, "power_profile_ws_idle_invalid"
                runtime["ws_idle_timeout_ms"] = timeout
            if "persist_ws_commands" in profile:
                persist_ws = _safe_bool(profile.get("persist_ws_commands"))
                if persist_ws is None:
                    return False, "power_profile_persist_ws_invalid"
                runtime["persist_ws_commands"] = persist_ws
            runtime["power_profile"] = "custom"
            changed = True
        else:
            return False, "power_profile_invalid_type"

    if "runtime" in payload:
        return False, "runtime_update_not_allowed"

    return True, "config_applied" if changed else "config_no_changes"


def _next_sleep_from_obj(cfg, obj, current):
    next_sleep = _local_heartbeat_sec(cfg, value=current)
    if obj and isinstance(obj, dict) and "next_pull_sec" in obj:
        cand = _safe_int(obj.get("next_pull_sec"), None)
        if cand is not None:
            min_sec = _safe_int(cfg.get("next_pull_min_sec", 30), 30)
            max_sec = _safe_int(cfg.get("next_pull_max_sec", 86400), 86400)
            next_sleep = _clamp(cand, min_sec, max_sec)
    return next_sleep


def _log_debug(client, *args):
    if getattr(client, "operational_debug", getattr(client, "debug", 0)):
        try:
            print(*args)
        except Exception:
            pass


def _ack_both(client, device_id, token, cmd_id, notes, ws=None, ok=True):
    ts = now_iso_utc_or_none(client.hal)
    ack_http_ok = False
    try:
        st, obj, dbg = client.ack_command(device_id, token, cmd_id, ack_at=ts, notes=notes or "")
        ack_http_ok = st in (200, 201)
    except Exception:
        ack_http_ok = False

    ack_ws_ok = False
    try:
        if ws and hasattr(ws, "send_ack") and ws.can_ack():
            ack_ws_ok = bool(ws.send_ack(cmd_id=cmd_id, ok=bool(ok), notes=notes or ""))
    except Exception:
        ack_ws_ok = False
    _log_debug(client, "[ack] id=", cmd_id, "http=", ack_http_ok, "ws=", ack_ws_ok, "ok=", bool(ok))
    return ack_http_ok, ack_ws_ok


def _result(local_ok, ack_http_ok=False, ack_ws_ok=False, completed=True, notes=""):
    return {
        "local_ok": bool(local_ok),
        "ack_http_ok": bool(ack_http_ok),
        "ack_ws_ok": bool(ack_ws_ok),
        "completed": bool(completed),
        "notes": notes or "",
    }


def _result_ok(status):
    return status in (200, 201)


def _report_documented_state_event(client, device_id, token, event_id, ts, success_status, failure_note=None, extra=None):
    if success_status:
        return _emit_event(
            client,
            device_id,
            token,
            status=success_status,
            severity="info",
            event_id=event_id,
            ts=ts,
            extra=extra,
        )
    return None, None, failure_note


def _validate_command_payload(cmd_type, payload):
    payload = payload or {}
    if not isinstance(payload, dict):
        return False, "payload_invalid_type", {}

    if cmd_type in ("open_actuator", "close_actuator", "ping", "snapshot", "instantanea"):
        return True, "ok", payload

    if cmd_type == "pulse_actuator":
        ms = _safe_int(payload.get("duration_ms", 500), None)
        if ms is None or ms <= 0 or ms > 10000:
            return False, "pulse_invalid_duration", {}
        return True, "ok", {"duration_ms": ms}

    if cmd_type == "update_config":
        return True, "ok", payload

    if cmd_type == "test_event":
        status = payload.get("status", "tamper_alert")
        severity = payload.get("severity", "warning")
        if not isinstance(status, str) or not status:
            return False, "event_status_invalid", {}
        if severity not in ("info", "warning", "critical", "emergency"):
            return False, "event_severity_invalid", {}
        return True, "ok", payload

    return True, "ok", payload


def _emit_event(client, device_id, token, status, severity, event_id, ts=None, extra=None, queue_on_fail=None):
    if queue_on_fail is None:
        queue_on_fail = _event_delivery_policy(status, severity)
    return client.report_event(
        device_id,
        token,
        status=status,
        severity=severity,
        event_id=event_id,
        ts=ts,
        extra=extra,
        queue_on_fail=queue_on_fail,
    )


def _event_status_note(status_code):
    return "ok" if _result_ok(status_code) else "fail:{}".format(status_code)


def _handle_sensor_transition(client, actuator, device_id, token, transition, ts=None):
    if not transition:
        return []

    statuses = []
    kind = transition.get("kind")
    reason = transition.get("reason")
    now_ts = ts or now_iso_utc_or_none(client.hal)
    event_id_base = "ev-{}".format(transition.get("at_ms", client.hal.ticks_ms()))

    _log_debug(
        client,
        "[sensor]",
        kind,
        "door=",
        transition.get("door_state"),
        "security=",
        transition.get("security_state"),
        "reason=",
        reason,
    )

    if kind == "door_opened_authorized":
        st_event, _, _ = _emit_event(
            client,
            device_id,
            token,
            status="device_opened",
            severity="info",
            event_id=event_id_base + "-opened",
            ts=now_ts,
            extra={"reason": "authorized_open"},
        )
        statuses.append(("device_opened", st_event))
        return statuses

    if kind == "door_closed":
        st_event, _, _ = _emit_event(
            client,
            device_id,
            token,
            status="device_closed",
            severity="info",
            event_id=event_id_base + "-closed",
            ts=now_ts,
            extra={"reason": "door_closed"},
        )
        statuses.append(("device_closed", st_event))
        return statuses

    if kind == "door_open_on_boot_unknown":
        st_event, _, _ = _emit_event(
            client,
            device_id,
            token,
            status="tamper_alert",
            severity="warning",
            event_id=event_id_base + "-boot-open",
            ts=now_ts,
            extra={"reason": reason or "open_on_boot_unknown"},
            queue_on_fail=True,
        )
        statuses.append(("tamper_alert", st_event))
        return statuses

    if kind in ("door_forced_open", "tamper_level"):
        st_tamper, _, _ = _emit_event(
            client,
            device_id,
            token,
            status="tamper_alert",
            severity="warning",
            event_id=event_id_base + "-tamper",
            ts=now_ts,
            extra={"reason": reason or "door_forced", "security_event_id": event_id_base},
            queue_on_fail=True,
        )
        st_unauth, _, _ = _emit_event(
            client,
            device_id,
            token,
            status="unauthorized_access",
            severity="emergency",
            event_id=event_id_base,
            ts=now_ts,
            extra={"reason": reason or "door_forced", "security_event_id": event_id_base},
            queue_on_fail=True,
        )
        statuses.append(("tamper_alert", st_tamper))
        statuses.append(("unauthorized_access", st_unauth))
        return statuses

    return statuses


def handle_command(cmd, client, actuator, cfg, device_id, token, ws=None):
    tstamp = now_iso_utc_or_none(client.hal)
    _log_debug(client, "[cmd>>] id=", cmd.id, "type=", cmd.type, "source=", cmd.source or "unknown")
    ok_payload, note_payload, normalized_payload = _validate_command_payload(cmd.type, cmd.payload)
    if not ok_payload:
        ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, note_payload, ws=ws, ok=False)
        return _result(False, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes=note_payload)

    if cmd.type == "open_actuator":
        st_auth_req, _, _ = _emit_event(
            client,
            device_id,
            token,
            status="authorization_request",
            severity="info",
            event_id="{}-authreq".format(cmd.id),
            ts=tstamp,
            extra={"source": cmd.source or "command"},
        )
        ok, notes = True, "Actuator opened"
        try:
            actuator.open()
            _log_debug(client, "[actuator] command=open", actuator.state_snapshot())
        except Exception as e:
            ok, notes = False, "open_failed:{}".format(e)
        st_auth_granted = None
        if ok:
            st_auth_granted, _, _ = _emit_event(
                client,
                device_id,
                token,
                status="authorization_granted",
                severity="info",
                event_id="{}-authgranted".format(cmd.id),
                ts=tstamp,
                extra={
                    "source": cmd.source or "command",
                    "security_state": actuator.state_snapshot().get("security_state"),
                },
            )
        note = "{};auth_request:{};auth_granted:{};report:{}".format(
            notes,
            _event_status_note(st_auth_req),
            _event_status_note(st_auth_granted) if st_auth_granted is not None else "skip",
            "deferred_to_sensor",
        )
        ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, note, ws=ws, ok=ok)
        _log_debug(client, "[cmd<<] id=", cmd.id, "completed=", True, "ok=", ok, "notes=", note)
        return _result(ok, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes=note)

    if cmd.type == "close_actuator":
        ok, notes = True, "Actuator closed"
        try:
            actuator.close()
            _log_debug(client, "[actuator] command=close", actuator.state_snapshot())
        except Exception as e:
            ok, notes = False, "close_failed:{}".format(e)
        note = "{};report:{}".format(notes, "deferred_to_sensor")
        ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, note, ws=ws, ok=ok)
        _log_debug(client, "[cmd<<] id=", cmd.id, "completed=", True, "ok=", ok, "notes=", note)
        return _result(ok, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes=note)

    if cmd.type == "pulse_actuator":
        st_auth_req, _, _ = _emit_event(
            client,
            device_id,
            token,
            status="authorization_request",
            severity="info",
            event_id="{}-authreq".format(cmd.id),
            ts=tstamp,
            extra={"source": cmd.source or "command", "mode": "pulse"},
        )
        ms = normalized_payload["duration_ms"]
        ok, notes = True, "actuator_pulsed_{}ms".format(ms)
        try:
            actuator.pulse(ms=ms)
            _log_debug(client, "[actuator] command=pulse", actuator.state_snapshot(), "duration_ms=", ms)
        except Exception as e:
            ok, notes = False, "pulse_failed:{}".format(e)
        st_auth_granted = None
        if ok:
            st_auth_granted, _, _ = _emit_event(
                client,
                device_id,
                token,
                status="authorization_granted",
                severity="info",
                event_id="{}-authgranted".format(cmd.id),
                ts=tstamp,
                extra={"source": cmd.source or "command", "mode": "pulse"},
            )
        report_note = "skip"
        note = "{};auth_request:{};auth_granted:{};report:{}".format(
            notes,
            _event_status_note(st_auth_req),
            _event_status_note(st_auth_granted) if st_auth_granted is not None else "skip",
            report_note,
        )
        ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, note, ws=ws, ok=ok)
        _log_debug(client, "[cmd<<] id=", cmd.id, "completed=", True, "ok=", ok, "notes=", note)
        return _result(ok, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes=note)

    if cmd.type == "update_config":
        ok_cfg, note = _apply_update_config(cfg, normalized_payload)
        if ok_cfg:
            persisted = save_config(cfg)
            ok_cfg = bool(persisted)
            note = "{};persisted:{}".format(note, "ok" if persisted else "fail")
        ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, note, ws=ws, ok=ok_cfg)
        _log_debug(client, "[cmd<<] id=", cmd.id, "completed=", True, "ok=", ok_cfg, "notes=", note)
        return _result(ok_cfg, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes=note)

    if cmd.type == "ping":
        telemetry = _base_telemetry(client)
        note_parts = ["pong", "uptime={}".format(client.hal.ticks_ms() // 1000)]
        if "battery_v" in telemetry:
            note_parts.append("battery_v={}".format(telemetry["battery_v"]))
        if "battery_pct" in telemetry:
            note_parts.append("battery_pct={}".format(telemetry["battery_pct"]))
        if ws:
            note_parts.append("ws={}".format("ready" if ws.can_ack() else "not_ready"))
        if telemetry.get("telemetry_missing"):
            note_parts.append("missing={}".format(",".join(telemetry["telemetry_missing"])))
        note = ";".join(note_parts)
        ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, note, ws=ws, ok=True)
        _log_debug(client, "[cmd<<] id=", cmd.id, "completed=", True, "ok=", True, "notes=", note)
        return _result(True, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes=note)

    if cmd.type in ("snapshot", "instantanea"):
        if not cfg.get("features", {}).get("allow_snapshot", True):
            ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, "snapshot_disabled", ws=ws, ok=False)
            return _result(False, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes="snapshot_disabled")
        snap = _base_telemetry(client)
        snap["uptime"] = client.hal.ticks_ms() // 1000
        try:
            st = actuator.is_open()
            if st is not None:
                snap["door_open"] = bool(st)
        except Exception:
            pass

        stc, obj, dbg = client.send_snapshot(device_id, token, snap)
        ok_snapshot = _result_ok(stc)
        ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, "snapshot:{}".format(stc), ws=ws, ok=ok_snapshot)
        _log_debug(client, "[cmd<<] id=", cmd.id, "completed=", True, "ok=", ok_snapshot, "notes=", "snapshot:{}".format(stc))
        return _result(ok_snapshot, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes="snapshot:{}".format(stc))

    if cmd.type == "test_event":
        p = normalized_payload
        status = p.get("status", "tamper_alert")
        severity = p.get("severity", "warning")
        extra = p.get("extra", {})
        st_event, obj_event, dbg_event = _emit_event(
            client,
            device_id,
            token,
            status=status,
            severity=severity,
            event_id=cmd.id,
            ts=tstamp,
            extra=extra,
        )
        ok_event = _result_ok(st_event)
        ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, "event sent:{}".format(st_event), ws=ws, ok=ok_event)
        _log_debug(client, "[cmd<<] id=", cmd.id, "completed=", True, "ok=", ok_event, "notes=", "event sent:{}".format(st_event))
        return _result(ok_event, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes="event sent:{}".format(st_event))

    ack_http_ok, ack_ws_ok = _ack_both(client, device_id, token, cmd.id, "unsupported:{}".format(cmd.type), ws=ws, ok=False)
    _log_debug(client, "[cmd<<] id=", cmd.id, "completed=", True, "ok=", False, "notes=", "unsupported:{}".format(cmd.type))
    return _result(False, ack_http_ok=ack_http_ok, ack_ws_ok=ack_ws_ok, completed=True, notes="unsupported:{}".format(cmd.type))


def handle_periodic_tasks(client, actuator, device_id, token, last_hb_ms, next_sleep):
    ts = now_iso_utc_or_none(client.hal)
    while True:
        transition = actuator.poll_transition()
        if not transition:
            break

        if transition.get("kind") in ("door_forced_open", "tamper_level"):
            now_ms = transition.get("at_ms", client.hal.ticks_ms())
            suppress_ms = _safe_int(client.cfg.get("runtime", {}).get("tamper_repeat_suppress_ms", 15000), 15000)
            last_tamper_ms = client.runtime_state.get("last_tamper_event_ms")
            can_emit = last_tamper_ms is None
            if last_tamper_ms is not None:
                last_tamper_ms = _safe_int(last_tamper_ms, now_ms)
                can_emit = client.hal.ticks_diff(now_ms, last_tamper_ms) >= suppress_ms
            if can_emit:
                client.runtime_state["last_tamper_event_ms"] = now_ms
                _handle_sensor_transition(client, actuator, device_id, token, transition, ts=ts)
            else:
                _log_debug(client, "[sensor] tamper suprimido", transition)
            continue

        _handle_sensor_transition(client, actuator, device_id, token, transition, ts=ts)

    battery_v, battery_pct = _read_battery_metrics(client)
    base_telemetry = _base_telemetry(client)
    low_threshold = _safe_float(client.cfg.get("thresholds", {}).get("low_battery_v", 3.3), 3.3)
    low_hysteresis = _safe_float(client.cfg.get("battery", {}).get("low_hysteresis_v", 0.05), 0.05)
    if battery_v is not None:
        low_active = bool(client.runtime_state.get("battery_low_active"))
        if battery_v <= low_threshold and not low_active:
            client.runtime_state["battery_low_active"] = True
            _emit_event(
                client,
                device_id,
                token,
                status="battery_low",
                severity="warning",
                event_id="battery-low-{}".format(client.hal.ticks_ms()),
                ts=ts,
                extra={
                    "battery_v": round(float(battery_v), 3),
                    "battery_pct": battery_pct,
                    "telemetry_missing": base_telemetry.get("telemetry_missing", []),
                },
            )
        elif low_active and battery_v >= (low_threshold + low_hysteresis):
            client.runtime_state["battery_low_active"] = False

    elapsed = client.hal.ticks_diff(client.hal.ticks_ms(), last_hb_ms)
    if elapsed >= (next_sleep * 1000):
        heartbeat_extra = {}
        if "gps_source" in base_telemetry:
            heartbeat_extra["gps_source"] = base_telemetry["gps_source"]
        if "telemetry_missing" in base_telemetry:
            heartbeat_extra["telemetry_missing"] = base_telemetry["telemetry_missing"]
        st, obj, dbg = client.heartbeat(
            device_id,
            token,
            battery_v=battery_v,
            battery_pct=battery_pct,
            latitude=base_telemetry.get("latitude"),
            longitude=base_telemetry.get("longitude"),
            extra=heartbeat_extra,
        )
        if isinstance(obj, dict):
            try:
                client.runtime_state["last_heartbeat_next_pull_sec"] = obj.get("next_pull_sec")
            except Exception:
                pass
        client.note_heartbeat_status(st, flush_device_id=device_id, flush_token=token)
        next_sleep = _next_sleep_from_obj(client.cfg, obj, next_sleep)
        last_hb_ms = client.hal.ticks_ms()
        _log_debug(
            client,
            "[heartbeat] status=",
            st,
            "next=",
            next_sleep,
            "failures=",
            client.runtime_state.get("heartbeat_failures", 0),
            "offline_queued=",
            bool(client.runtime_state.get("device_offline_queued", False)),
        )

    return last_hb_ms, next_sleep


def pull_all_commands(ws, feeder, max_ws=10, max_file=10):
    cmds = []
    if ws:
        ws.tick(max_reads=3)
        try:
            cmds_ws = ws.pull(max_n=max_ws)
        except Exception:
            cmds_ws = []
        cmds.extend(cmds_ws)

    try:
        cmds_file = feeder.pull(max_n=max_file)
    except Exception:
        cmds_file = []
    cmds.extend(cmds_file)
    return cmds

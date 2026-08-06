try:
    import ujson as json
except Exception:
    import json
import os


def _deep_copy(obj):
    if isinstance(obj, dict):
        out = {}
        for k in obj:
            out[k] = _deep_copy(obj[k])
        return out
    if isinstance(obj, list):
        return [_deep_copy(x) for x in obj]
    return obj


def _merge_dict(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge_dict(dst[k], v)
        else:
            dst[k] = _deep_copy(v)


def _safe_remove(path):
    try:
        os.remove(path)
    except Exception:
        pass


DEFAULT = {
    "host": "api.infrakey.fasttrack.cloud",
    "connect_host": None,
    "port": 443,
    "ws_host": None,
    "ws_connect_host": None,
    "nb_band": 28,
    "apn_fallback": "m2m.entel.cl",
    "user_agent": "pico-sim7080g/caopen-infrakey-1.3",
    "fw": "v1.0.0",
    "model": "SIM7080G-Pico",
    "latitude": -33.4489,
    "longitude": -70.6693,
    "heartbeat_interval_sec": 86400,
    "heartbeat_interval_min_sec": 60,
    "heartbeat_interval_max_sec": 86400,
    "next_pull_min_sec": 30,
    "next_pull_max_sec": 86400,
    "ws_enabled": False,
    "http_retry_count": 2,
    "http_retry_backoff_ms": 1200,
    "runtime": {
        "max_command_queue": 32,
        "loop_sleep_ms": 100,
        "loop_error_backoff_ms": 500,
        "supervisor_restart_delay_ms": 5000,
        "supervisor_catch_base_exceptions": True,
        "status_log_interval_sec": 30,
        "gc_collect_interval_sec": 60,
        "gc_log_free": True,
        "ws_pull_max": 10,
        "ws_queue_max": 32,
        "ws_buffer_max_bytes": 4096,
        "journal_pull_max": 10,
        "journal_max_bytes": 65536,
        "legacy_import_batch": 1,
        "outbox_flush_max": 5,
        "outbox_max_bytes": 65536,
        "offline_after_heartbeat_failures": 3,
        "heartbeat_failure_retry_sec": 60,
        "healthcheck_on_startup": True,
        "heartbeat_on_startup": True,
        "http_open_timeout_ms": 45000,
        "http_read_timeout_ms": 20000,
        "healthcheck_timeout_fail_open": True,
        "journal_state_save_every": 1,
        "journal_compact_min_bytes": 4096,
        "journal_compact_ratio_pct": 50,
        "journal_enabled": True,
        "persist_ws_commands": False,
        "power_profile": "balanced",
        "processed_id_cache_size": 128,
        "ws_reconnect_delay_ms": 5000,
        "ws_idle_timeout_ms": 90000,
        "ws_confirm_timeout_ms": 12000,
        "ws_reconnect_fail_reset_threshold": 3,
        "ws_reconnect_fail_modem_reset_threshold": 6,
        "ws_down_heartbeat_sec": 0,
        "heartbeat_effective_max_sec": 0,
        "ws_identifier_include_device_id": False,
        "ws_token_in_query": False,
        "tamper_repeat_suppress_ms": 15000,
        "debug_operational_enabled": True,
        "debug_modem_enabled": False,
        "scheduled_reboot_enabled": False,
        "scheduled_reboot_interval_sec": 21600,
        "scheduled_reboot_only_when_idle": True,
        "scheduled_reboot_min_uptime_sec": 3600,
        "scheduled_reboot_send_heartbeat": True,
    },
    "features": {
        "allow_snapshot": True,
    },
    "battery": {
        "adc_pin": None,
        "divider_ratio": 2.0,
        "vref": 3.3,
        "samples": 4,
        "empty_v": 3.3,
        "full_v": 4.2,
        "low_hysteresis_v": 0.05,
    },
    "gps": {
        "mode": "static_config",
        "allow_static": True,
        "include_source": True,
        "power_on_startup": True,
        "power_down_after_read": True,
        "poll_attempts": 2,
        "poll_interval_ms": 1000,
        "cache_ms": 15000,
        "retry_ms": 300000,
        "allow_stale_cache": True,
    },
    "hardware": {
        "uart_port": 0,
        "baud": 115200,
        "led_pin": 25,
        "pwr_en_pin": 14,
        "uart_tx_pin": None,
        "uart_rx_pin": None,
    },
    # GPIO
    "gpio": {
        "actuator_pin": 12,
        "actuator_active_high": True,
        "actuator_pulse_ms": 500,
        "servo_pwm_pin": 20,
        "servo_freq": 50,
        "servo_open_us": 2300,
        "servo_close_us": 700,
        "servo_drive_ms": 500,
        "sensor_pin": 15,
        "sensor_open_is": 1,
        "sensor_pull": "down",
        "sensor_debounce_ms": 60,
        "sensor_authorized_open_ms": 8000,
        "sensor_alert_if_open_on_boot": True,
        "sensor_boot_grace_ms": 1000,
        "tamper_pin": 15,
        "tamper_pull": "up",
        "tamper_active_high": True,
    },
    "thresholds": {"low_battery_v": 3.3},
    "files": {
        "token": "token.json",
        "outbox": "outbox.jsonl",
        "outbox_state": "outbox_state.json",
        "commands_journal": "commands_queue.jsonl",
        "commands_state": "commands_state.json",
        "commands_dead_letter": "commands_dead.jsonl",
        "commands_legacy_inbox": "commands_inbox",
    },
}


def load_config(path="device_config.json"):
    cfg = _deep_copy(DEFAULT)
    if path and path in os.listdir():
        try:
            with open(path, "r") as f:
                user = json.loads(f.read() or "{}")
            if isinstance(user, dict):
                _merge_dict(cfg, user)
        except Exception:
            pass
    return cfg


def _persistable_config(cfg):
    data = {
        "host": cfg.get("host"),
        "connect_host": cfg.get("connect_host"),
        "port": cfg.get("port"),
        "ws_host": cfg.get("ws_host"),
        "ws_connect_host": cfg.get("ws_connect_host"),
        "nb_band": cfg.get("nb_band"),
        "apn_fallback": cfg.get("apn_fallback"),
        "user_agent": cfg.get("user_agent"),
        "fw": cfg.get("fw"),
        "model": cfg.get("model"),
        "latitude": cfg.get("latitude"),
        "longitude": cfg.get("longitude"),
        "heartbeat_interval_sec": cfg.get("heartbeat_interval_sec"),
        "heartbeat_interval_min_sec": cfg.get("heartbeat_interval_min_sec"),
        "heartbeat_interval_max_sec": cfg.get("heartbeat_interval_max_sec"),
        "next_pull_min_sec": cfg.get("next_pull_min_sec"),
        "next_pull_max_sec": cfg.get("next_pull_max_sec"),
        "ws_enabled": cfg.get("ws_enabled"),
        "runtime": _deep_copy(cfg.get("runtime", {})),
        "features": _deep_copy(cfg.get("features", {})),
        "battery": _deep_copy(cfg.get("battery", {})),
        "gps": _deep_copy(cfg.get("gps", {})),
        "hardware": _deep_copy(cfg.get("hardware", {})),
        "gpio": _deep_copy(cfg.get("gpio", {})),
        "thresholds": _deep_copy(cfg.get("thresholds", {})),
        "files": _deep_copy(cfg.get("files", {})),
        "http_retry_count": cfg.get("http_retry_count"),
        "http_retry_backoff_ms": cfg.get("http_retry_backoff_ms"),
    }
    return data


def save_config(cfg, path="device_config.json"):
    tmp = path + ".tmp"
    bak = path + ".bak"
    try:
        payload = json.dumps(_persistable_config(cfg))
        with open(tmp, "w") as f:
            f.write(payload)
        try:
            if path in os.listdir():
                try:
                    os.rename(path, bak)
                except Exception:
                    _safe_remove(bak)
                    os.rename(path, bak)
        except Exception:
            pass
        os.rename(tmp, path)
        _safe_remove(bak)
        return True
    except Exception:
        _safe_remove(tmp)
        return False

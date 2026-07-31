try:
    import sys
    if "/" not in sys.path:
        sys.path.append("/")
    if "/lib" not in sys.path:
        sys.path.append("/lib")
except Exception:
    pass

try:
    import gc
except Exception:
    gc = None

try:
    import utime as _time
except Exception:
    import time as _time

from sim7080mini.config import load_config
from sim7080mini.infrakey import InfrakeyClient
from sim7080mini.actuator import Actuator
from sim7080mini.commandfeeder import (
    CommandPipeline,
    JsonlCommandJournal,
    LegacyInboxImporter,
)
from sim7080mini.ws_feeder import WebSocketCommandFeeder
from sim7080mini.modem import SIM7080
from sim7080mini.handlers import (
    _local_heartbeat_sec,
    _read_battery_metrics,
    handle_command,
    handle_periodic_tasks,
)


def _bounded_sleep(value, fallback):
    try:
        v = int(value)
        return v if v > 0 else fallback
    except Exception:
        return fallback


def _safe_next_sleep(cfg, obj, fallback):
    base = _local_heartbeat_sec(cfg, value=cfg.get("heartbeat_interval_sec", fallback), fallback=fallback)
    min_sec = cfg.get("next_pull_min_sec", 30)
    max_sec = cfg.get("next_pull_max_sec", 86400)
    runtime = cfg.get("runtime", {})
    effective_max = runtime.get("heartbeat_effective_max_sec", 0)
    try:
        effective_max = int(effective_max)
    except Exception:
        effective_max = 0
    local_cap = _local_heartbeat_sec(cfg, value=cfg.get("heartbeat_interval_sec", fallback), fallback=fallback)
    if effective_max > 0 and effective_max > local_cap:
        local_cap = effective_max
    try:
        max_sec = min(int(max_sec), int(local_cap))
    except Exception:
        max_sec = local_cap
    next_sleep = base
    if obj and isinstance(obj, dict) and "next_pull_sec" in obj:
        try:
            cand = int(obj["next_pull_sec"])
            if cand < int(min_sec):
                cand = int(min_sec)
            if cand > int(max_sec):
                cand = int(max_sec)
            next_sleep = cand
        except Exception:
            next_sleep = base
    return _bounded_sleep(next_sleep, fallback)


def _make_ws(client, cfg, token, device_id, debug):
    ws_debug = cfg.get("ws_debug", getattr(client, "modem_debug", debug))
    ws_host = cfg.get("ws_host", cfg["host"])
    if not ws_host:
        ws_host = cfg["host"]
    ws_port = cfg.get("ws_port", cfg["port"])
    ws_connect_host = cfg.get("ws_connect_host", cfg.get("connect_host"))
    runtime = cfg.get("runtime", {})
    ws_max_queue = runtime.get("ws_queue_max", runtime.get("max_command_queue", 32))
    identifier_extra = {}
    if runtime.get("ws_identifier_include_device_id", False):
        identifier_extra["device_id"] = device_id
    ws_modem = SIM7080(client.hal, nb_band=cfg["nb_band"], tls_ctx=1, sock_id=1, debug=ws_debug)
    return WebSocketCommandFeeder(
        modem=ws_modem,
        hal=client.hal,
        host=ws_host,
        port=ws_port,
        token=token,
        identifier_extra=identifier_extra,
        debug=ws_debug,
        sock_id=1,
        max_queue=ws_max_queue,
        token_in_query=runtime.get("ws_token_in_query", False),
        connect_host=ws_connect_host,
    )


def _collect_gc():
    if gc:
        try:
            gc.collect()
        except Exception:
            pass


def _sleep_ms(ms):
    try:
        _time.sleep_ms(int(ms))
    except Exception:
        _time.sleep(int(ms) / 1000.0)


def _run_session():
    cfg = load_config("device_config.json")
    debug = cfg.get("debug", False)
    runtime = cfg.get("runtime", {})
    print(
        "[boot] fw=",
        cfg.get("fw"),
        "profile=",
        runtime.get("power_profile", "unknown"),
        "ws_enabled=",
        bool(cfg.get("ws_enabled", False)),
        "journal_enabled=",
        bool(runtime.get("journal_enabled", True)),
    )

    client = InfrakeyClient(cfg, debug=debug)
    actuator = Actuator(client.hal, cfg["gpio"])

    journal = JsonlCommandJournal(
        journal_path=cfg["files"]["commands_journal"],
        state_path=cfg["files"]["commands_state"],
        dead_letter_path=cfg["files"]["commands_dead_letter"],
        debug=debug,
        state_save_every=runtime.get("journal_state_save_every", 1),
        processed_id_cache_size=runtime.get("processed_id_cache_size", 128),
    )
    legacy_importer = None
    if runtime.get("journal_enabled", True):
        legacy_importer = LegacyInboxImporter(
            inbox_dir=cfg["files"]["commands_legacy_inbox"],
            journal=journal,
            debug=debug,
        )

    if not client.bringup():
        print("[supervisor] bringup fallido; se reintentara")
        return False

    if runtime.get("healthcheck_on_startup", True):
        h_st, h_obj, h_dbg = client.health()
        if h_st in (200, 201):
            print("[health] ok status=", h_st)
        if h_st not in (200, 201):
            print("[health] status=", h_st)
            if not runtime.get("healthcheck_timeout_fail_open", True):
                print("[supervisor] healthcheck bloquea arranque; se reintentara")
                return False

    device_id, token = client.claim_if_needed()
    if not (device_id and token):
        print("No se obtuvo token/device_id. Abortando.")
        return False
    device_id, token = client.current_credentials(device_id, token)

    print("[claim] ok device_id=", device_id)

    battery_v, battery_pct = _read_battery_metrics(client)
    st, obj, dbg = client.heartbeat(device_id, token, battery_v=battery_v, battery_pct=battery_pct)
    device_id, token = client.current_credentials(device_id, token)
    client.note_heartbeat_status(st, flush_device_id=device_id, flush_token=token)

    next_sleep = _safe_next_sleep(cfg, obj, cfg.get("heartbeat_interval_sec", 86400))
    print("[heartbeat] startup status=", st, "next_pull_sec=", next_sleep)
    last_hb_ms = client.hal.ticks_ms()
    ws = None
    last_ws_attempt_ms = 0
    ws_reconnect_delay_ms = _bounded_sleep(runtime.get("ws_reconnect_delay_ms", 5000), 5000)
    ws_idle_timeout_ms = _bounded_sleep(runtime.get("ws_idle_timeout_ms", 90000), 90000)
    ws_confirm_timeout_ms = _bounded_sleep(runtime.get("ws_confirm_timeout_ms", 12000), 12000)
    status_log_interval_sec = _bounded_sleep(runtime.get("status_log_interval_sec", 30), 30)
    ws_reconnect_fail_reset_threshold = _bounded_sleep(runtime.get("ws_reconnect_fail_reset_threshold", 3), 3)
    ws_reconnect_fail_modem_reset_threshold = _bounded_sleep(runtime.get("ws_reconnect_fail_modem_reset_threshold", 6), 6)
    last_status_log_ms = 0
    ws_reconnect_failures = 0
    ws_down_since_ms = None
    if cfg.get("ws_enabled", False):
        ws = _make_ws(client, cfg, token, device_id, debug)
        last_ws_attempt_ms = client.hal.ticks_ms()
        if not ws.connect():
            print("[ws] no disponible; continuo con journal/cola RAM")
            ws = None
            ws_down_since_ms = last_ws_attempt_ms

    pipeline = CommandPipeline(
        ws=ws,
        journal=journal if runtime.get("journal_enabled", True) else None,
        legacy_importer=legacy_importer,
        max_queue=runtime.get("max_command_queue", 32),
        debug=debug,
        persist_ws_commands=runtime.get("persist_ws_commands", False),
    )
    if debug:
        print(
            "[commands] journal_enabled=",
            bool(runtime.get("journal_enabled", True)),
            "persist_ws_commands=",
            bool(runtime.get("persist_ws_commands", False)),
        )

    loop_sleep_ms = _bounded_sleep(runtime.get("loop_sleep_ms", 100), 100)
    loop_error_backoff_ms = _bounded_sleep(runtime.get("loop_error_backoff_ms", 500), 500)
    legacy_import_batch = max(0, _bounded_sleep(runtime.get("legacy_import_batch", 1), 1))
    journal_compact_min_bytes = max(0, _bounded_sleep(runtime.get("journal_compact_min_bytes", 4096), 4096))
    journal_compact_ratio_pct = max(1, _bounded_sleep(runtime.get("journal_compact_ratio_pct", 50), 50))

    try:
        while True:
            try:
                cmds = pipeline.pull(
                    max_out=runtime.get("max_command_queue", 32),
                    max_ws=runtime.get("ws_pull_max", 10),
                    max_journal=runtime.get("journal_pull_max", 10),
                    import_legacy=legacy_import_batch,
                )
                for cmd in cmds:
                    result = handle_command(cmd, client, actuator, cfg, device_id, token, ws=ws)
                    if result is None:
                        result = {"local_ok": False, "ack_http_ok": False, "ack_ws_ok": False, "completed": False}
                    if bool(result.get("completed", False)):
                        pipeline.mark_processed(cmd.id, result=result)
                    elif debug:
                        print("[cmd] no completado, queda recuperable:", cmd.id, cmd.type, result)

                prev_device_id, prev_token = device_id, token
                device_id, token = client.current_credentials(device_id, token)
                if ws and token != prev_token:
                    print("[ws] token actualizado; reiniciando canal")
                    try:
                        ws.close()
                    except Exception:
                        pass
                    ws = None
                    pipeline.ws = None
                    last_ws_attempt_ms = 0

                last_hb_ms, next_sleep = handle_periodic_tasks(
                    client, actuator, device_id, token, last_hb_ms, next_sleep
                )
                device_id, token = client.current_credentials(device_id, token)

                if cfg.get("ws_enabled", False):
                    now_ms = client.hal.ticks_ms()
                    ws_ok = bool(ws and ws.is_healthy(
                        idle_timeout_ms=ws_idle_timeout_ms,
                        confirm_timeout_ms=ws_confirm_timeout_ms,
                    ))
                    if ws and not ws_ok:
                        print("[ws] canal no saludable; reiniciando")
                        try:
                            ws.close()
                        except Exception:
                            pass
                        ws = None
                        pipeline.ws = None
                        if ws_down_since_ms is None:
                            ws_down_since_ms = now_ms

                    if (not ws) and client.hal.ticks_diff(now_ms, last_ws_attempt_ms) >= ws_reconnect_delay_ms:
                        last_ws_attempt_ms = now_ms
                        try:
                            ws = _make_ws(client, cfg, token, device_id, debug)
                            if ws.connect():
                                print("[ws] reconectado")
                                pipeline.ws = ws
                                ws_reconnect_failures = 0
                                ws_down_since_ms = None
                            else:
                                print("[ws] reconexion fallida")
                                ws = None
                                pipeline.ws = None
                                ws_reconnect_failures += 1
                        except Exception as exc:
                            print("[ws] error reconectando:", exc)
                            ws = None
                            pipeline.ws = None
                            ws_reconnect_failures += 1

                        if ws is None and ws_reconnect_failures >= ws_reconnect_fail_reset_threshold:
                            restart_modem = ws_reconnect_failures >= ws_reconnect_fail_modem_reset_threshold
                            print(
                                "[ws] recovery escalado failures=",
                                ws_reconnect_failures,
                                "restart_modem=",
                                restart_modem,
                            )
                            client.recover_connectivity(restart_modem=restart_modem)
                            _collect_gc()

                if runtime.get("journal_enabled", True):
                    journal.compact_if_needed(
                        min_bytes=journal_compact_min_bytes,
                        min_ratio_pct=journal_compact_ratio_pct,
                    )

                now_ms = client.hal.ticks_ms()
                if status_log_interval_sec > 0 and client.hal.ticks_diff(now_ms, last_status_log_ms) >= (status_log_interval_sec * 1000):
                    last_status_log_ms = now_ms
                    state = {}
                    try:
                        state = actuator.state_snapshot()
                    except Exception:
                        state = {}
                    ws_state = "disabled"
                    if cfg.get("ws_enabled", False):
                        ws_state = "ready" if (ws and ws.is_healthy(idle_timeout_ms=ws_idle_timeout_ms, confirm_timeout_ms=ws_confirm_timeout_ms)) else "down"
                    queue_size = None
                    ws_stats = {}
                    try:
                        queue_size = pipeline.stats().get("queue", {}).get("queued")
                    except Exception:
                        queue_size = None
                    try:
                        ws_stats = ws.stats() if ws else {}
                    except Exception:
                        ws_stats = {}
                    if getattr(client, "operational_debug", getattr(client, "debug", 0)):
                        print(
                            "[state]",
                            "actuator_state=",
                            state.get("actuator_state"),
                            "door_state=",
                            state.get("door_state"),
                            "security_state=",
                            state.get("security_state"),
                            "ws=",
                            ws_state,
                            "last_hb=",
                            client.runtime_state.get("last_heartbeat_status"),
                            "next_hb=",
                            next_sleep,
                            "queue=",
                            queue_size,
                            "ws_ping_delta=",
                            ws_stats.get("actioncable_pings_delta", 0),
                            "ws_retries=",
                            ws_reconnect_failures,
                            "tamper_suppressed=",
                            bool(client.runtime_state.get("last_tamper_event_ms")),
                        )
                client.hal.sleep_ms(loop_sleep_ms)
            except Exception as exc:
                print("[loop] error:", exc)
                try:
                    print("[pipeline] stats:", pipeline.stats())
                except Exception:
                    pass
                _collect_gc()
                client.hal.sleep_ms(loop_error_backoff_ms)
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        try:
            actuator.deinit()
        except Exception:
            pass
        _collect_gc()
    return True


def main():
    last_reason = None
    while True:
        try:
            ok = _run_session()
            if ok:
                last_reason = "session_end"
            else:
                last_reason = "startup_retry"
        except Exception as exc:
            print("[supervisor] fatal:", exc)
            last_reason = "fatal_exception"
        _collect_gc()
        cfg = load_config("device_config.json")
        delay_ms = _bounded_sleep(cfg.get("runtime", {}).get("supervisor_restart_delay_ms", 5000), 5000)
        print("[supervisor] restart in", delay_ms, "ms reason=", last_reason)
        _sleep_ms(delay_ms)


if __name__ == "__main__":
    main()

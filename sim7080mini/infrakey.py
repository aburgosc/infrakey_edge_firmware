try:
    import ujson as json
except Exception:
    import json
import os
from .hal import make_hal
from .modem import SIM7080
from .httpclient import HttpClient
from .outbox import JsonlEventOutbox


def _safe_int(v, default):
    try:
        return int(v)
    except Exception:
        return default


class InfrakeyClient:
    def __init__(self, cfg: dict, debug=1):
        self.cfg = cfg
        self.debug = debug
        hw = cfg.get("hardware", {})
        self.hal = make_hal(
            debug=debug,
            uart_port=hw.get("uart_port", 0),
            baud=hw.get("baud", 115200),
            led_pin=hw.get("led_pin", 25),
            pwr_en_pin=hw.get("pwr_en_pin", 14),
            uart_tx_pin=hw.get("uart_tx_pin"),
            uart_rx_pin=hw.get("uart_rx_pin"),
        )
        self.modem = SIM7080(self.hal, nb_band=cfg["nb_band"], tls_ctx=0, sock_id=0, debug=debug)
        self.http = HttpClient(
            self.modem,
            host=cfg["host"],
            port=cfg["port"],
            user_agent=cfg["user_agent"],
            connect_host=cfg.get("connect_host"),
        )
        self.token_file = cfg["files"]["token"]
        self.model = cfg["model"]
        self.fw = cfg["fw"]
        self.latitude = cfg["latitude"]
        self.longitude = cfg["longitude"]
        self.http_retry_count = max(1, _safe_int(cfg.get("http_retry_count", 2), 2))
        self.http_retry_backoff_ms = max(0, _safe_int(cfg.get("http_retry_backoff_ms", 1200), 1200))
        self.device_id = None
        self.auth_token = None
        outbox_path = cfg.get("files", {}).get("outbox", "outbox.jsonl")
        outbox_state = cfg.get("files", {}).get("outbox_state", outbox_path + ".state.json")
        self.outbox = JsonlEventOutbox(outbox_path=outbox_path, state_path=outbox_state, debug=debug)
        self.runtime_state = {
            "heartbeat_failures": 0,
            "device_offline_queued": False,
            "battery_low_active": False,
            "last_heartbeat_status": None,
            "last_heartbeat_ok": None,
            "last_heartbeat_next_pull_sec": None,
        }
        self._gps_cache = {
            "at_ms": None,
            "payload": None,
        }

    def _log(self, *args):
        if self.debug:
            try:
                print(*args)
            except Exception:
                pass

    # persistencia
    def _load_token(self):
        try:
            if self.token_file in os.listdir():
                with open(self.token_file, "r") as f:
                    obj = json.loads(f.read() or "{}")
                if isinstance(obj, dict):
                    return obj
        except Exception:
            pass
        return None

    def _save_token(self, device_id, auth_token):
        tmp = self.token_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(json.dumps({"device_id": device_id, "auth_token": auth_token}))
            try:
                if self.token_file in os.listdir():
                    os.remove(self.token_file)
            except Exception:
                pass
            os.rename(tmp, self.token_file)
            return True
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False

    def clear_token(self):
        self.device_id = None
        self.auth_token = None
        try:
            os.remove(self.token_file)
        except Exception:
            pass

    def current_credentials(self, fallback_device_id=None, fallback_auth_token=None):
        return (
            self.device_id or fallback_device_id,
            self.auth_token or fallback_auth_token,
        )

    # bring-up
    def bringup(self):
        self.hal.blink()
        if not self.modem.start():
            self._log("No arranca el modem")
            return False
        gps_cfg = self.cfg.get("gps", {})
        gps_mode = gps_cfg.get("mode", "static_config")
        power_on_startup = bool(gps_cfg.get("power_on_startup", True))
        power_down_after_read = bool(gps_cfg.get("power_down_after_read", True))
        if gps_mode in ("modem_gnss", "prefer_modem") and power_on_startup and not power_down_after_read:
            try:
                self.modem.ensure_gnss_power(True)
            except Exception:
                pass
        self.modem.set_radio_nbiot()
        if gps_mode in ("modem_gnss", "prefer_modem"):
            try:
                location = self.resolve_location(force=True)
                if self.debug and location:
                    self._log("[gps] cache primed before network:", location)
            except Exception:
                pass
        if not self.modem.attach_and_pdp(fallback_apn=self.cfg["apn_fallback"]):
            self._log("No fue posible adjuntar PDP/red")
            return False
        return True

    def resolve_location(self, force=False):
        gps_cfg = self.cfg.get("gps", {})
        gps_mode = gps_cfg.get("mode", "static_config")
        allow_static = bool(gps_cfg.get("allow_static", True))
        include_source = bool(gps_cfg.get("include_source", True))

        if not force:
            try:
                cache_ms = max(0, _safe_int(gps_cfg.get("cache_ms", 15000), 15000))
                cached_at = self._gps_cache.get("at_ms")
                if cached_at is not None and self.hal.ticks_diff(self.hal.ticks_ms(), cached_at) < cache_ms:
                    cached_payload = self._gps_cache.get("payload")
                    if isinstance(cached_payload, dict):
                        return dict(cached_payload)
            except Exception:
                pass

        payload = {}
        if gps_mode in ("modem_gnss", "prefer_modem"):
            power_down_after_read = bool(gps_cfg.get("power_down_after_read", True))
            try:
                info = self.modem.read_gnss_location(
                    ensure_power=True,
                    attempts=max(1, _safe_int(gps_cfg.get("poll_attempts", 2), 2)),
                    delay_ms=max(0, _safe_int(gps_cfg.get("poll_interval_ms", 1000), 1000)),
                )
            except Exception:
                info = None
            finally:
                if power_down_after_read:
                    try:
                        self.modem.ensure_gnss_power(False)
                    except Exception:
                        pass
            if isinstance(info, dict):
                lat = info.get("latitude")
                lon = info.get("longitude")
                if lat is not None and lon is not None:
                    payload["latitude"] = lat
                    payload["longitude"] = lon
                    if include_source:
                        payload["gps_source"] = "modem_gnss"

        if not payload and allow_static:
            if self.latitude is not None:
                payload["latitude"] = self.latitude
            if self.longitude is not None:
                payload["longitude"] = self.longitude
            if ("latitude" in payload or "longitude" in payload) and include_source:
                payload["gps_source"] = "static_config"

        self._gps_cache["at_ms"] = self.hal.ticks_ms()
        self._gps_cache["payload"] = dict(payload)
        return payload

    def _valid_identity(self, imei, iccid):
        if not imei or not iccid:
            return False
        if imei == "000000000000000":
            return False
        if iccid == "00000000000000000000":
            return False
        return True

    def _should_retry_status(self, status):
        status = _safe_int(status, 0)
        if status <= 0:
            return True
        if status in (408, 409, 425, 429):
            return True
        if status >= 500:
            return True
        return False

    def _request_with_retry(self, fn, op_name):
        last = (0, None, "")
        for attempt in range(1, self.http_retry_count + 1):
            last = fn()
            status = last[0]
            if status in (200, 201, 202, 204):
                return last
            if attempt < self.http_retry_count and self._should_retry_status(status):
                self._log("[http]", op_name, "retry", attempt, "status=", status)
                self.hal.sleep_ms(self.http_retry_backoff_ms)
                continue
            break
        return last

    def _refresh_credentials(self, op_name):
        self._log("[auth]", op_name, "401 -> refreshing credentials")
        device_id, auth_token = self.claim_if_needed(force_refresh=True)
        return bool(device_id and auth_token)

    def _authorized_request(self, op_name, request_fn, device_id=None, auth_token=None, allow_refresh=True):
        device_id, auth_token = self.current_credentials(device_id, auth_token)
        if not (device_id and auth_token):
            return 401, {"error": "missing_credentials"}, ""

        result = self._request_with_retry(
            lambda: request_fn(device_id, auth_token),
            op_name,
        )
        if result[0] == 401 and allow_refresh and self._refresh_credentials(op_name):
            device_id, auth_token = self.current_credentials(device_id, auth_token)
            result = self._request_with_retry(
                lambda: request_fn(device_id, auth_token),
                op_name + "_after_refresh",
            )
        return result

    # claim/heartbeat
    def claim_if_needed(self, force_refresh=False):
        if force_refresh:
            self.clear_token()
        tok = self._load_token()
        if tok and "device_id" in tok and "auth_token" in tok:
            self.device_id = tok["device_id"]
            self.auth_token = tok["auth_token"]
            return tok["device_id"], tok["auth_token"]

        imei = self.hal.get_imei() or ""
        iccid = self.hal.get_iccid() or ""
        if not self._valid_identity(imei, iccid):
            self._log("Identidad de modem invalida. IMEI/ICCID no confiables.")
            return None, None

        body = {
            "imei": imei,
            "iccid": iccid,
            "model": self.model,
            "fw": self.fw,
        }
        location = self.resolve_location(force=False)
        if "latitude" in location:
            body["latitude"] = location["latitude"]
        if "longitude" in location:
            body["longitude"] = location["longitude"]
        if self.debug:
            self._log("[claim] payload=", body)
        status, obj, dbg = self._request_with_retry(
            lambda: self.http.post_json("/api/v1/devices/claim", body),
            "claim",
        )
        if status in (200, 201) and obj and "device_id" in obj and "auth_token" in obj:
            self._save_token(obj["device_id"], obj["auth_token"])
            self.device_id = obj["device_id"]
            self.auth_token = obj["auth_token"]
            self._log("[claim] success device_id=", obj["device_id"])
            return obj["device_id"], obj["auth_token"]

        self._log("Claim fallo. Status=", status)
        self._log(dbg)
        return None, None

    def health(self):
        return self._request_with_retry(
            lambda: self.http.get_json("/api/v1/health"),
            "health",
        )

    def heartbeat(self, device_id, auth_token, battery_v=None, battery_pct=None, latitude=None, longitude=None, extra=None):
        body = {
            "lte": "OK",
            "fw": self.fw,
        }
        if latitude is None or longitude is None:
            location = self.resolve_location(force=False)
            if latitude is None:
                latitude = location.get("latitude")
            if longitude is None:
                longitude = location.get("longitude")
        if latitude is not None:
            body["latitude"] = latitude
        if longitude is not None:
            body["longitude"] = longitude
        if battery_v is not None:
            body["battery_v"] = battery_v
        if battery_pct is not None:
            body["battery_pct"] = battery_pct
        if isinstance(extra, dict) and extra:
            body.update(extra)
        headers = {"Authorization": "Bearer {}".format(auth_token)}
        def _send(req_device_id, req_token):
            headers["Authorization"] = "Bearer {}".format(req_token)
            path = "/api/v1/devices/{}/heartbeat".format(req_device_id)
            if self.debug:
                self._log(path, body)
            return self.http.post_json(path, body, headers=headers)
        return self._authorized_request(
            "heartbeat",
            _send,
            device_id=device_id,
            auth_token=auth_token,
        )

    # events / snapshot / ack
    def queue_event(self, status, severity, event_id, ts=None, extra=None):
        body = {"status": status, "severity": severity, "event_id": event_id}
        if ts is not None:
            body["ts"] = ts
        if extra:
            body["extra"] = extra
        return self.outbox.enqueue(body)

    def flush_event_outbox(self, device_id, auth_token, max_n=5):
        def _sender(record):
            status, obj, dbg = self._authorized_request(
                "event_outbox",
                lambda req_device_id, req_token: self.http.post_json(
                    "/api/v1/devices/{}/events".format(req_device_id),
                    record,
                    headers={"Authorization": "Bearer {}".format(req_token)},
                ),
                device_id=device_id,
                auth_token=auth_token,
            )
            return status in (200, 201)

        flushed = self.outbox.flush(_sender, max_n=max_n)
        self.outbox.compact_if_needed()
        if flushed and self.debug:
            self._log("[outbox] flushed=", flushed)
        return flushed

    def note_heartbeat_status(self, status, flush_device_id=None, flush_token=None):
        self.runtime_state["last_heartbeat_status"] = status
        self.runtime_state["last_heartbeat_ok"] = bool(status in (200, 201))
        if status in (200, 201):
            if self.debug and self.runtime_state.get("heartbeat_failures", 0):
                self._log("[heartbeat] recovered after failures=", self.runtime_state.get("heartbeat_failures", 0))
            self.runtime_state["heartbeat_failures"] = 0
            self.runtime_state["device_offline_queued"] = False
            if flush_device_id and flush_token:
                try:
                    flushed = self.flush_event_outbox(
                        flush_device_id,
                        flush_token,
                        max_n=_safe_int(self.cfg.get("runtime", {}).get("outbox_flush_max", 5), 5),
                    )
                    if self.debug and flushed:
                        self._log("[heartbeat] outbox flushed after recovery =", flushed)
                except Exception:
                    pass
            return

        failures = _safe_int(self.runtime_state.get("heartbeat_failures", 0), 0) + 1
        self.runtime_state["heartbeat_failures"] = failures
        threshold = max(1, _safe_int(self.cfg.get("runtime", {}).get("offline_after_heartbeat_failures", 3), 3))
        if self.debug:
            self._log("[heartbeat] failure status=", status, "count=", failures, "threshold=", threshold)
        if failures >= threshold and not self.runtime_state.get("device_offline_queued"):
            self.runtime_state["device_offline_queued"] = True
            if self.debug:
                self._log("[heartbeat] queueing device_offline after failures=", failures)
            self.queue_event(
                status="device_offline",
                severity="critical",
                event_id="offline-{}".format(self.hal.ticks_ms()),
                ts=getattr(self.hal, "now_utc_iso", lambda: None)(),
                extra={"reason": "heartbeat_failures", "count": failures},
            )

    def report_event(self, device_id, auth_token, status, severity, event_id, ts=None, extra=None, queue_on_fail=False):
        body = {"status": status, "severity": severity, "event_id": event_id}
        if ts is not None:
            body["ts"] = ts
        if extra:
            body["extra"] = extra
        result = self._authorized_request(
            "event",
            lambda req_device_id, req_token: self.http.post_json(
                "/api/v1/devices/{}/events".format(req_device_id),
                body,
                headers={"Authorization": "Bearer {}".format(req_token)},
            ),
            device_id=device_id,
            auth_token=auth_token,
        )
        if queue_on_fail and result[0] not in (200, 201):
            try:
                self.queue_event(status=status, severity=severity, event_id=event_id, ts=ts, extra=extra)
                if self.debug:
                    self._log("[event] queued for retry:", status, event_id)
            except Exception:
                pass
        return result

    def send_snapshot(self, device_id, auth_token, snapshot_dict):
        if self.debug:
            self._log("[snapshot] payload=", snapshot_dict)
        return self._authorized_request(
            "snapshot",
            lambda req_device_id, req_token: self.http.post_json(
                "/api/v1/devices/{}/events/snapshot".format(req_device_id),
                snapshot_dict,
                headers={"Authorization": "Bearer {}".format(req_token)},
            ),
            device_id=device_id,
            auth_token=auth_token,
        )

    def ack_command(self, device_id, auth_token, cmd_id, ack_at=None, notes=""):
        body = {"notes": notes}
        if ack_at is not None:
            body["ack_at"] = ack_at
        return self._authorized_request(
            "ack",
            lambda req_device_id, req_token: self.http.post_json(
                "/api/v1/devices/{}/commands/{}/ack".format(req_device_id, cmd_id),
                body,
                headers={"Authorization": "Bearer {}".format(req_token)},
            ),
            device_id=device_id,
            auth_token=auth_token,
        )

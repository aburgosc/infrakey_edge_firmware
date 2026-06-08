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
        self.hal = make_hal(debug=debug)
        self.modem = SIM7080(self.hal, nb_band=cfg["nb_band"], tls_ctx=0, sock_id=0, debug=debug)
        self.http = HttpClient(
            self.modem, host=cfg["host"], port=cfg["port"], user_agent=cfg["user_agent"]
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
        self.modem.set_radio_nbiot()
        if not self.modem.attach_and_pdp(fallback_apn=self.cfg["apn_fallback"]):
            self._log("No fue posible adjuntar PDP/red")
            return False
        return True

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
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
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
        if latitude is None:
            latitude = self.latitude
        if longitude is None:
            longitude = self.longitude
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

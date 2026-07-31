# mock.py
# Simulador del SIM7080G (basado en tu mock ajustado)

import json
import os


def _getenv(name, default=None):
    try:
        return os.getenv(name, default)
    except Exception:
        return default


def _safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return default

class SIM7080Modem:
    def __init__(self, debug=1):
        self.debug = debug
        self.echo = False
        self.cmee = 2
        self.creg = 1
        self.csq = (20, 99)
        self.cgatt = 1
        self.apn = "m2m.mock.cl"
        self.TLS_CTX = 0
        self.SOCK_ID = 0
        self.sock_open = False
        self.sock_proto = "TCP"
        self.sock_host = None
        self.sock_port = None
        self._tx = bytearray()
        self._rx = bytearray()

    def _reset_buffers(self):
        del self._rx[:]
        del self._tx[:]

    def _log(self, *a):
        if self.debug >= 1:
            print(*a)

    def at(self, cmd, expect="OK", timeout=1500, dump=False):
        c = cmd.strip()
        if not c.startswith("AT"): c = "AT" + c
        if self.debug >= 1: print(">>", c)
        resp_lines = []

        if c in ("AT", "ATZ", "AT&F"): resp_lines.append("OK")
        elif c == "ATE0": self.echo=False; resp_lines.append("OK")
        elif c == "ATE1": self.echo=True; resp_lines.append("OK")
        elif c == "AT+CMEE=2": self.cmee=2; resp_lines.append("OK")
        elif c == "AT+CPIN?": resp_lines += ["+CPIN: READY", "OK"]
        elif c == "AT+CSQ": resp_lines += [f"+CSQ: {self.csq[0]},{self.csq[1]}", "OK"]
        elif c == "AT+CEREG?": resp_lines += [f"+CEREG: 0,{self.creg}", "OK"]
        elif c == "AT+CGNAPN": resp_lines += [f'+CGNAPN: 1,"{self.apn}"', "OK"]
        elif c.startswith("AT+CNACT="): resp_lines.append("OK")
        elif c == "AT+CNACT?": resp_lines += ['+CNACT: 0,1,"10.0.0.10"', "OK"]
        elif c.startswith('AT+CSSLCFG="') or c.startswith("AT+CSSLCFG="): resp_lines.append("OK")
        elif c.startswith("AT+CASSLCFG="): resp_lines.append("OK")
        elif c.startswith("AT+CAOPEN="):
            try:
                body = c.split("=",1)[1]
                parts = self._parse_csv(body)
                cid = int(parts[0]); ptype = int(parts[1])
                if len(parts)>=5 and parts[2].strip('"').upper()=="TCP":
                    host = parts[3].strip('"'); port = int(parts[4])
                else:
                    host = parts[2].strip('"'); port = int(parts[3])
                self.SOCK_ID = cid; self.sock_open = True; self.sock_proto = "TCP" if ptype==0 else "UDP"
                self.sock_host = host; self.sock_port = port
                resp_lines += [f"+CAOPEN: {cid},0", "OK"]
            except Exception:
                self.sock_open = False; resp_lines.append("ERROR")
        elif c == "AT+CASTATE?":
            st = 1 if self.sock_open else 0
            resp_lines += [f"+CASTATE: {self.SOCK_ID},{st}", "OK"]
        elif c.startswith("AT+CACLOSE="):
            self.sock_open=False; self._reset_buffers()
            resp_lines += [f"+CACLOSE: {self.SOCK_ID},0", "OK"]
        elif c.startswith("AT+CASEND="):
            resp_lines.append("> " if self.sock_open else "ERROR")
        elif c.startswith("AT+CARECV="):
            try:
                maxlen = int(c.split(",",1)[1].split(",")[-1])
                if not self._rx: resp_lines += ["+CARECV: 0", "OK"]
                else:
                    out = bytes(self._rx[:maxlen]); del self._rx[:maxlen]
                    resp_lines += [f"+CARECV: {len(out)},{out.decode('utf-8','ignore')}", "OK"]
            except Exception:
                resp_lines.append("ERROR")
        else:
            resp_lines.append("OK")

        resp_text = "\r\n".join(resp_lines) + ("\r\n" if resp_lines else "")
        if self.debug >= 2 and resp_text:
            for ln in resp_text.splitlines(): print("<<", ln)

        if expect is None: return True, resp_text
        if resp_text and expect in resp_text: return True, resp_text
        if resp_text and "ERROR" in resp_text: return False, resp_text
        return False, resp_text

    def _build_http(self, code=201, body_dict=None):
        body = json.dumps(body_dict or {"message":"ok","code":code})
        head = [
            f"HTTP/1.1 {code} {'Created' if code==201 else 'OK'}",
            "Cache-Control: max-age=0, private, must-revalidate",
            f"Content-Length: {len(body.encode('utf-8'))}",
            "Content-Type: application/json; charset=utf-8",
            "Connection: close",
            "", ""
        ]
        return ("\r\n".join(head) + body).encode("utf-8")

    def _http_reply_for_request(self, data):
        txt = data.decode("utf-8","ignore")
        first = (txt.splitlines() or [""])[0]
        parts = first.split()
        method = parts[0] if len(parts)>=1 else ""
        path = parts[1] if len(parts)>=2 else ""
        health_status = _safe_int(_getenv("SIM7080_MOCK_HEALTH_STATUS", 200), 200)
        claim_status = _safe_int(_getenv("SIM7080_MOCK_CLAIM_STATUS", 201), 201)
        heartbeat_status = _safe_int(_getenv("SIM7080_MOCK_HEARTBEAT_STATUS", 201), 201)
        heartbeat_next_pull = _safe_int(_getenv("SIM7080_MOCK_NEXT_PULL_SEC", 86400), 86400)
        event_status = _safe_int(_getenv("SIM7080_MOCK_EVENT_STATUS", 201), 201)
        snapshot_status = _safe_int(_getenv("SIM7080_MOCK_SNAPSHOT_STATUS", 201), 201)
        ack_status = _safe_int(_getenv("SIM7080_MOCK_ACK_STATUS", 201), 201)
        # respuestas:
        if method=="POST" and path=="/api/v1/devices/claim":
            body = {"device_id":"DEV-MOCK-123","auth_token":"TOKEN-MOCK-ABC"} if claim_status in (200, 201) else {"error":"claim_failed"}
            return self._build_http(claim_status, body)
        if method=="POST" and path.endswith("/heartbeat"):
            body = {"ok": True, "next_pull_sec": heartbeat_next_pull} if heartbeat_status in (200, 201) else {"error":"heartbeat_failed"}
            return self._build_http(heartbeat_status, body)
        if method=="POST" and path.endswith("/events"):
            body = {"ok": True, "stored": True} if event_status in (200, 201) else {"error":"event_failed"}
            return self._build_http(event_status, body)
        if method=="POST" and path.endswith("/events/snapshot"):
            body = {"ok": True, "snapshot": True} if snapshot_status in (200, 201) else {"error":"snapshot_failed"}
            return self._build_http(snapshot_status, body)
        if method=="POST" and "/commands/" in path and path.endswith("/ack"):
            body = {"ok": True, "ack": True} if ack_status in (200, 201) else {"error":"ack_failed"}
            return self._build_http(ack_status, body)
        if method=="GET" and path=="/api/v1/health":
            body = {"ok": True} if health_status in (200, 201) else {"error":"health_failed"}
            return self._build_http(health_status, body)
        # por defecto
        return self._build_http(201, {"message":"ok"})

    def socket_open(self, host, port=443, timeout_ms=60000):
        self.socket_close()
        self.sock_open=True; self.sock_host=host; self.sock_port=port
        if self.debug>=1: self._log(f"[mock] socket_open -> {host}:{port}")
        return True

    def socket_close(self):
        self.sock_open=False; self._reset_buffers()

    def socket_send(self, payload, wait_ok_ms=8000):
        if not self.sock_open: return False
        data = payload if isinstance(payload,(bytes,bytearray)) else str(payload).encode("utf-8")
        self._tx += data
        if data[:8].upper().startswith((b"GET ",b"POST ",b"PUT ",b"DELE")):
            self._rx += self._http_reply_for_request(data)
        else:
            self._rx += b"OK\r\n"
        return True

    def carecv_once_exact(self, ask_len, overall_ms=8000):
        if not self._rx: return b""
        out = bytes(self._rx[:ask_len]); del self._rx[:ask_len]
        return out

    def write_at_only(self, data):
        return True

    def http_post_json_return(self, host, port, user_agent, path, body_dict, extra_headers=None, connect_host=None, open_timeout_ms=None):
        req = "POST {} HTTP/1.1\r\nHost: {}\r\nUser-Agent: {}\r\n\r\n{}".format(
            path,
            host,
            user_agent,
            json.dumps(body_dict or {}),
        ).encode("utf-8")
        reply = self._http_reply_for_request(req)
        return self._parse_http_reply(reply)

    def http_get_json_return(self, host, port, user_agent, path, extra_headers=None, connect_host=None, open_timeout_ms=None):
        req = "GET {} HTTP/1.1\r\nHost: {}\r\nUser-Agent: {}\r\n\r\n".format(
            path,
            host,
            user_agent,
        ).encode("utf-8")
        reply = self._http_reply_for_request(req)
        return self._parse_http_reply(reply)

    def _parse_http_reply(self, raw):
        try:
            text = raw.decode("utf-8", "ignore")
            head, body = text.split("\r\n\r\n", 1)
            first = head.splitlines()[0]
            status = int(first.split()[1])
            try:
                obj = json.loads(body or "{}")
            except Exception:
                obj = None
            return status, obj, text
        except Exception:
            return 0, None, ""

    def get_imei(self): return "359123456789012"
    def get_iccid(self): return "8940012345678901234"
    def ensure_gnss_power(self, enabled=True): return True
    def read_gnss_info(self):
        return {
            "run_status": 1,
            "fix_status": 1,
            "utc": "2026-07-28T12:00:00Z",
            "latitude": -33.4489,
            "longitude": -70.6693,
            "altitude": 520.0,
            "speed": 0.0,
            "course": 0.0,
            "satellites": 8,
            "raw": "+CGNSINF: mock",
        }
    def read_gnss_location(self, ensure_power=True, attempts=1, delay_ms=1000):
        return self.read_gnss_info()

    def _parse_csv(self, s):
        parts=[]; cur=""; inq=False
        for ch in s:
            if ch=='"' and not inq: inq=True; cur+=ch
            elif ch=='"' and inq: inq=False; cur+=ch
            elif ch==',' and not inq: parts.append(cur.strip()); cur=""
            else: cur+=ch
        if cur: parts.append(cur.strip())
        return parts

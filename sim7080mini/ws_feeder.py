# WebSocket feeder para ActionCable: wss://<host>/cable
# - Handshake RFC6455
# - Frames Text (JSON), Ping/Pong (cliente enmascara)
# - Suscripcion a DeviceCommandsChannel
# - Cola de comandos con interfaz pull()
try:
    import ujson as json
except Exception:
    import json

try:
    import ubinascii as _binascii
except Exception:
    import binascii as _binascii

try:
    import uhashlib as _hashlib
except Exception:
    import hashlib as _hashlib

try:
    import urandom as _urandom
except Exception:
    import os as _os, random as _random
    def _randbytes(n):
        try:
            return _os.urandom(n)
        except Exception:
            return bytes([_random.getrandbits(8) for _ in range(n)])
else:
    def _randbytes(n):
        return _urandom.randbytes(n) if hasattr(_urandom, "randbytes") else bytes([_urandom.getrandbits(8) for _ in range(n)])

_WS_PATH = "/cable"
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def _b64_str(raw: bytes) -> str:
    try:
        return _binascii.b2a_base64(raw).decode().strip()
    except Exception:
        import base64
        return base64.b64encode(raw).decode().strip()

def _sha1(data: bytes) -> bytes:
    h = _hashlib.sha1()
    h.update(data)
    return h.digest()

def _safe_decode(b: bytes, max_len=None):
    if max_len is not None:
        b = b[:max_len]
    try:
        return b.decode("utf-8", "ignore")
    except Exception:
        return ""

def _hex_preview(b: bytes, max_n=96):
    try:
        x = b[:max_n]
        return (_binascii.hexlify(x).decode() if hasattr(_binascii, "hexlify") else x.hex())
    except Exception:
        return ""

class Command:
    def __init__(self, cmd_id: str, cmd_type: str, payload=None):
        self.id = cmd_id
        self.type = cmd_type
        self.payload = payload or {}
        self.source = "ws"

class WebSocketCommandFeeder:
    """
    TLS con el modem, upgrade WebSocket, Suscripcion a DeviceCommandsChannel
    y entrega comandos via pull(). ACK opcional por WS.
    """
    def __init__(self, modem, hal, host, port=443, token=None, identifier_extra=None, debug=1, sock_id=1, max_queue=32, token_in_query=False, connect_host=None):
        self.m = modem
        self.hal = hal
        self.host = host
        self.port = port
        self.connect_host = connect_host
        self.token = token
        self.identifier_extra = identifier_extra or {}  # e.g. {"device_id": ...}
        self.debug = debug
        self.connected = False
        self.subscribed = False
        self._buf = b""
        self._queue = []
        self.max_queue = max(1, int(max_queue or 32))
        self._dropped = 0
        self._last_ping_ms = 0
        self._last_recv_bytes = 0
        self._last_rx_ms = 0
        self._connected_at_ms = 0
        self.sock_id = int(sock_id)  # CID dedicado para WS (por defecto 1)
        self._identifier_str = None  # guardamos el identifier usado en subscribe
        self.token_in_query = bool(token_in_query)

    def _enqueue_command(self, cmd):
        if len(self._queue) >= self.max_queue:
            self._dropped += 1
            self._log1("[ws] queue overflow drop id=", getattr(cmd, "id", "?"))
            return False
        self._queue.append(cmd)
        return True

    # ---------- logging helpers ----------
    def _log1(self, *a):
        if self.debug >= 1:
            try: print(*a)
            except Exception: pass

    def _log2(self, *a):
        if self.debug >= 2:
            try: print(*a)
            except Exception: pass

    def _redact_bytes(self, data):
        safe = data if isinstance(data, bytes) else bytes(data)
        if not self.token:
            return safe
        try:
            token_bytes = str(self.token).encode()
            if token_bytes:
                safe = safe.replace(token_bytes, b"<redacted>")
        except Exception:
            pass
        return safe

    # ---------- CID save/restore ----------
    def _swap_to_ws_cid(self):
        prev = getattr(self.m, "SOCK_ID", 0)
        try:
            self.m.SOCK_ID = self.sock_id
            if hasattr(self.hal, "set_sock_id"):
                self.hal.set_sock_id(self.sock_id)
        except Exception:
            pass
        return prev

    def _restore_cid(self, prev):
        try:
            self.m.SOCK_ID = prev
            if hasattr(self.hal, "set_sock_id"):
                self.hal.set_sock_id(prev)
        except Exception:
            pass

    # ---------- envio/recepcion ----------
    def _send(self, data: bytes) -> bool:
        prev = self._swap_to_ws_cid()
        try:
            safe_preview = self._redact_bytes(data)
            self._log2("[ws>>raw] len=", len(data), " preview(hex)=", _hex_preview(safe_preview))
            ok = self.m.socket_send(data)
            if not ok:
                self._log1("[ws] socket_send fallo (len=", len(data), ")")
            return ok
        finally:
            self._restore_cid(prev)

    def _recv_some(self, ask=1460, ms=1200) -> bytes:
        prev = self._swap_to_ws_cid()
        try:
            b = self.m.carecv_once_exact(ask, overall_ms=ms) or b""
            if b:
                self._last_recv_bytes = len(b)
                self._last_rx_ms = self.hal.ticks_ms()
                self._log2("[ws<<raw] len=", len(b), " preview(hex)=", _hex_preview(b))
            return b
        except Exception as e:
            self._log1("[ws] recv error:", e)
            return b""
        finally:
            self._restore_cid(prev)

    # ---------- headers HTTP ----------
    def _parse_http_headers(self, raw: bytes):
        txt = _safe_decode(raw)
        lines = txt.split("\r\n")
        status = lines[0] if lines else ""
        headers = {}
        for ln in lines[1:]:
            if not ln or ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        return status, headers

    def _http_read_headers(self, timeout_ms=20000):
        acc = b""
        t0 = self.hal.ticks_ms()
        self._log1("[ws] esperando HTTP 101...")
        while self.hal.ticks_diff(self.hal.ticks_ms(), t0) < timeout_ms:
            chunk = self._recv_some(1460, 1200)
            if not chunk:
                self.hal.sleep_ms(60)
                continue
            acc += chunk
            sep = acc.find(b"\r\n\r\n")
            if sep != -1:
                hdr = acc[:sep + 4]
                leftover = acc[sep + 4:]
                self._log1("[ws] headers recibidos: bytes=", len(hdr), " leftover=", len(leftover))
                self._log2("[ws<<hdr]\n" + _safe_decode(hdr))
                return hdr, leftover
        self._log1("[ws] timeout leyendo headers, bytes=", len(acc))
        if acc:
            self._log2("[ws<<hdr:partial]\n" + _safe_decode(acc))
        return acc, b""

    def _validate_accept(self, key_b64: str, headers: dict):
        acc = headers.get("sec-websocket-accept")
        if not acc:
            return True
        try:
            expected = _b64_str(_sha1(key_b64.encode() + _WS_GUID))
            ok = (acc.strip() == expected)
            self._log2("[ws] Sec-WebSocket-Accept:", "OK" if ok else "MISMATCH")
            return ok
        except Exception:
            return True

    # ---------- conexion / subscribe ----------
    def connect(self) -> bool:
        self._log1("[ws] conectando wss://{}:{}{}".format(self.host, self.port, _WS_PATH))
        prev = self._swap_to_ws_cid()
        try:
            if not self.m.socket_open(self.host, self.port, connect_host=self.connect_host):
                self._log1("[ws] no se pudo abrir socket TLS")
                return False
        finally:
            self._restore_cid(prev)

        key_b64 = _b64_str(_randbytes(16))
        origin = "https://{}".format(self.host)
        path = _WS_PATH + (("?token={}".format(self.token)) if (self.token and self.token_in_query) else "")
        hdrs = [
            "GET {} HTTP/1.1".format(path),
            "Host: {}".format(self.host),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: {}".format(key_b64),
            "Sec-WebSocket-Version: 13",
            "Sec-WebSocket-Protocol: actioncable-v1-json",
            "Origin: {}".format(origin),
            "Cache-Control: no-cache",
            "Pragma: no-cache",
        ]
        if self.token:
            hdrs.append("Authorization: Bearer {}".format(self.token))
        req = ("\r\n".join(hdrs) + "\r\n\r\n").encode()
        self._log2("[ws>>]\n" + _safe_decode(self._redact_bytes(req)))
        if not self._send(req):
            self._log1("[ws] CASEND fallo (upgrade)")
            prev2 = self._swap_to_ws_cid()
            try:
                self.m.socket_close()
            except Exception:
                pass
            finally:
                self._restore_cid(prev2)
            return False

        raw_hdrs, leftover = self._http_read_headers()
        status_line, headers = self._parse_http_headers(raw_hdrs)

        ok_101 = False
        try:
            parts = (status_line or "").strip().split()
            ok_101 = (len(parts) >= 2 and parts[0].startswith("HTTP/") and parts[1] == "101")
        except Exception:
            ok_101 = False
        if (not ok_101) and (" 101 " in (status_line or "")):
            ok_101 = True

        self._log1("[ws] status:", status_line or "(desconocido)")
        if not ok_101:
            prev3 = self._swap_to_ws_cid()
            try:
                self.m.socket_close()
            except Exception:
                pass
            finally:
                self._restore_cid(prev3)
            self._log1("[ws] upgrade no fue 101")
            return False

        self._validate_accept(key_b64, headers)

        if leftover:
            self._log2("[ws] leftover posheader bytes=", len(leftover))
            self._buf += leftover
            self._parse_frames_into_queue()

        ident_dict = {"channel": "DeviceCommandsChannel"}
        ident_dict.update(self.identifier_extra)
        self._identifier_str = json.dumps(ident_dict, separators=(",", ":"))

        sub = {"command": "subscribe", "identifier": self._identifier_str}
        self._log1("[ws] suscribiendo:", self._identifier_str)
        if not self._send(self._build_frame_text(json.dumps(sub, separators=(",", ":")).encode())):
            self._log1("[ws] fallo envio subscribe")
            prev4 = self._swap_to_ws_cid()
            try:
                self.m.socket_close()
            except Exception:
                pass
            finally:
                self._restore_cid(prev4)
            return False

        self.connected = True
        self.subscribed = False
        self._connected_at_ms = self.hal.ticks_ms()
        self._last_rx_ms = self._connected_at_ms
        self._log1("[ws] socket abierto; esperando confirm_subscription")
        return True

    # ---------- frames ----------
    def _mask_client_frame(self, payload: bytes) -> bytes:
        mask = _randbytes(4)
        masked = bytearray(len(payload))
        for i, b in enumerate(payload):
            masked[i] = b ^ mask[i & 3]
        return bytes(mask), bytes(masked)

    def _build_frame_text(self, payload: bytes) -> bytes:
        b0 = 0x80 | 0x1
        ln = len(payload)
        if ln <= 125:
            header = bytes([b0, 0x80 | ln])
        elif ln <= 0xFFFF:
            header = bytes([b0, 0x80 | 126]) + ln.to_bytes(2, "big")
        else:
            header = bytes([b0, 0x80 | 127]) + ln.to_bytes(8, "big")
        mask, masked = self._mask_client_frame(payload)
        frame = header + mask + masked
        safe_payload = self._redact_bytes(payload)
        self._log2("[ws>>frame:text] len=", len(frame), " payload_len=", ln, " payload_preview=", _safe_decode(safe_payload, 120))
        return frame

    def _build_frame_ping(self, payload: bytes = b""):
        b0 = 0x80 | 0x9
        ln = len(payload)
        if ln <= 125:
            header = bytes([b0, 0x80 | ln])
        elif ln <= 0xFFFF:
            header = bytes([b0, 0x80 | 126]) + ln.to_bytes(2, "big")
        else:
            header = bytes([b0, 0x80 | 127]) + ln.to_bytes(8, "big")
        mask, masked = self._mask_client_frame(payload)
        frame = header + mask + masked
        self._log2("[ws>>frame:ping] len=", len(frame), " payload_len=", ln)
        return frame

    def _send_ws_text(self, s: str) -> bool:
        return self._send(self._build_frame_text(s.encode()))

    # ---------- ActionCable perform ----------
    def can_ack(self):
        return bool(self.connected and self.subscribed and self._identifier_str)

    def is_healthy(self, idle_timeout_ms=90000, confirm_timeout_ms=12000):
        if not self.connected:
            return False
        now = self.hal.ticks_ms()
        if not self.subscribed:
            if self.hal.ticks_diff(now, self._connected_at_ms) > int(confirm_timeout_ms):
                self._log1("[ws] sin confirm_subscription dentro de timeout")
                return False
            return True
        if self.hal.ticks_diff(now, self._last_rx_ms) > int(idle_timeout_ms):
            self._log1("[ws] idle timeout excedido")
            return False
        return True

    def perform(self, action: str, **kwargs):
        """
        Envia perform(action, **kwargs) usando el mismo identifier del subscribe.
        """
        if not self.can_ack():
            return False
        try:
            data_obj = {"action": action}
            data_obj.update(kwargs)
            envelope = {
                "command": "message",
                "identifier": self._identifier_str,
                "data": json.dumps(data_obj, separators=(",", ":"))
            }
            return self._send_ws_text(json.dumps(envelope, separators=(",", ":")))
        except Exception:
            return False

    def send_ack(self, cmd_id: str, ok: bool, notes: str = ""):
        """
        Atajo para perform('ack', id=..., ok=..., notes=...)
        """
        return self.perform("ack", id=cmd_id, ok=bool(ok), notes=notes or "")

    # ---------- parseo y cola ----------
    def _parse_frames_into_queue(self):
        out_msgs = []
        while True:
            if len(self._buf) < 2:
                break
            b0 = self._buf[0]
            b1 = self._buf[1]
            fin = (b0 & 0x80) != 0
            opcode = (b0 & 0x0F)
            masked = (b1 & 0x80) != 0
            plen = (b1 & 0x7F)
            pos = 2
            if plen == 126:
                if len(self._buf) < pos + 2:
                    break
                plen = int.from_bytes(self._buf[pos:pos + 2], "big"); pos += 2
            elif plen == 127:
                if len(self._buf) < pos + 8:
                    break
                plen = int.from_bytes(self._buf[pos:pos + 8], "big"); pos += 8
            if masked:
                # servidor NUNCA debe enmascarar
                self._buf = self._buf[pos:]
                continue
            if len(self._buf) < pos + plen:
                break

            payload = self._buf[pos:pos + plen]
            self._buf = self._buf[pos + plen:]

            self._log2("[ws<<frame] fin=", fin, " opcode=0x{:X}".format(opcode),
                       " plen=", plen, " preview=", _safe_decode(payload, 120))

            if opcode == 0x1:  # text
                try:
                    out_msgs.append(payload.decode("utf-8", "ignore"))
                except Exception:
                    pass
            elif opcode == 0x9:  # ping -> responder pong
                pong = bytes([0x80 | 0xA, len(payload)]) + payload
                # Debug minimo: no spamear, solo en nivel 2
                self._log2("[ws] ping recibido -> pong")
                self._send(pong)
            elif opcode == 0xA:  # pong
                self._log2("[ws] pong recibido (len={})".format(len(payload)))
            elif opcode == 0x8:  # close
                self._log1("[ws] close recibido")
                self.connected = False
                self.subscribed = False
                prev = self._swap_to_ws_cid()
                try:
                    self.m.socket_close()
                except Exception:
                    pass
                finally:
                    self._restore_cid(prev)
                break
            else:
                self._log2("[ws] opcode no manejado:", opcode)

        # ActionCable: welcome/confirm/ping/message
        for txt in out_msgs:
            self._log2("[ws<<txt]", txt)
            try:
                obj = json.loads(txt)
            except Exception:
                continue

            typ = obj.get("type") or ""
            if typ == "welcome":
                # nivel 1: silencio; nivel 2: visible
                self._log2("[ws] srv:", typ)
                continue
            if typ == "confirm_subscription":
                self.subscribed = True
                self._last_rx_ms = self.hal.ticks_ms()
                self._log1("[ws] suscripcion confirmada")
                continue
            if typ == "reject_subscription":
                self._log1("[ws] suscripcion rechazada:", txt)
                self.connected = False
                self.subscribed = False
                continue
            if typ == "ping":
                self._log1("[ws] actioncable ping")
                self._last_rx_ms = self.hal.ticks_ms()
                continue
            if typ and self.debug >= 1:
                self._log1("[ws] mensaje control:", txt)

            payload = None
            # ActionCable envia mensajes de canal como
            # {"identifier":"...","message":{...}} sin "type" superior.
            if typ == "message" or (not typ and isinstance(obj.get("message"), dict)):
                inner = obj.get("message")
                if isinstance(inner, dict):
                    # Legacy: {"message":{"type":"command","data":{...}}}
                    if inner.get("type") == "command" and isinstance(inner.get("data"), dict):
                        payload = inner["data"]
                    else:
                        # Actual: {"message":{"id":"â€¦","type":"open_actuator","payload":{...}}}
                        cmd_type = inner.get("type") or inner.get("command_type")
                        if cmd_type:
                            payload = {
                                "id": inner.get("id"),
                                "command_type": cmd_type,
                                "payload": inner.get("payload") or inner.get("data") or {}
                            }
            elif typ == "command" and isinstance(obj.get("data"), dict):
                payload = obj["data"]

            if payload and isinstance(payload, dict):
                cid = payload.get("id") or "cmd-{}".format(self.hal.ticks_ms())
                ctype = payload.get("command_type") or payload.get("type") or ""
                pdata = payload.get("payload") or payload.get("data") or {}
                if ctype:
                    self._log1("[ws] cmd:", ctype, "id=", cid)
                    self._enqueue_command(Command(cid, ctype, pdata))

    # ---------- ciclo ----------
    def tick(self, max_reads=3):
        if not self.connected:
            return
        if self._buf:
            self._parse_frames_into_queue()
        total_read = 0
        for _ in range(max_reads):
            chunk = self._recv_some(1460, 700)
            if not chunk:
                break
            total_read += len(chunk)
            self._buf += chunk
            self._parse_frames_into_queue()
        if total_read:
            self._log2("[ws] tick read_bytes=", total_read, " buf_len=", len(self._buf))

        # keepalive (cliente -> ping enmascarado) - nivel 2 para no spamear
        now = self.hal.ticks_ms()
        if self.hal.ticks_diff(now, self._last_ping_ms) > 25000:
            try:
                self._log2("[ws] keepalive -> ping")
                self._send(self._build_frame_ping(b""))
                self._last_ping_ms = now
            except Exception as e:
                self._log1("[ws] ping error:", e)

    def pull(self, max_n=5):
        out, self._queue = self._queue[:max_n], self._queue[max_n:]
        if out:
            self._log1("[ws] pull ->", len(out), "cmds")
        return out

    def stats(self):
        return {
            "connected": bool(self.connected),
            "subscribed": bool(self.subscribed),
            "queued": len(self._queue),
            "max_queue": self.max_queue,
            "dropped": self._dropped,
            "last_rx_ms": self._last_rx_ms,
            "last_ping_ms": self._last_ping_ms,
        }

    def close(self):
        self._log1("[ws] cerrando")
        try:
            self._send(bytes([0x88, 0x00]))  # close
        except Exception:
            pass
        prev = self._swap_to_ws_cid()
        try:
            self.m.socket_close()
        except Exception:
            pass
        finally:
            self._restore_cid(prev)
        self.connected = False
        self.subscribed = False
        self._buf = b""
        self._last_recv_bytes = 0
        self._log1("[ws] cerrado")



try:
    import ujson as json
except Exception:
    import json

import re

def _log_debug(enabled, *args):
    if enabled:
        try: print(*args)
        except Exception: pass


def _safe_float(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


def _safe_int(v, default=None):
    try:
        return int(v)
    except Exception:
        return default

class SIM7080:
    """
    Manejo de TLS/sockets/HTTP para SIM7080.
    Cambios clave:
      - attach_and_pdp(): +CSOCKSETPN=0
      - socket_open():   CDNSGIP previo y apertura CAOPEN con SNI
      - _tls_basic_setup(): TLS 1.2 con suites ECDSA compatibles
      - Timeouts ampliados y logs detallados
    """
    def __init__(self, hal, nb_band=28, tls_ctx=0, sock_id=0, debug=1):
        self.hal = hal
        self.nb_band = nb_band
        self.TLS_CTX = tls_ctx          # índice de contexto CSSLCFG (no es on/off)
        self.SOCK_ID = sock_id          # CID actual a usar
        self.debug = debug
        self.READ_TIMEOUT = 30000
        self.TLS_CIPHERS = ("0xC02B", "0xC02C")
        self._last_gnss_fix = None
        if hasattr(self.hal, "set_sock_id"):
            self.hal.set_sock_id(self.SOCK_ID)

    # --------- helpers ----------
    def _ensure_cid(self):
        if hasattr(self.hal, "set_sock_id"):
            try:
                self.hal.set_sock_id(self.SOCK_ID)
            except Exception:
                pass

    def _parse_cgnapn(self, s):
        try:
            for ln in s.splitlines():
                if "+CGNAPN:" in ln:
                    body = ln.split(":", 1)[1].strip()
                    ok = int(body.split(",")[0].strip())
                    apn = body.split(",")[1].strip().strip('"')
                    return ok, apn
        except Exception:
            pass
        return 0, ""

    def _parse_cgnsinf(self, s):
        try:
            for ln in s.splitlines():
                if "+CGNSINF:" not in ln:
                    continue
                body = ln.split(":", 1)[1].strip()
                parts = [p.strip() for p in body.split(",")]
                if len(parts) < 5:
                    continue
                run_status = _safe_int(parts[0], 0) or 0
                fix_status = _safe_int(parts[1], 0) or 0
                utc = parts[2] if len(parts) > 2 else ""
                lat = _safe_float(parts[3], None)
                lon = _safe_float(parts[4], None)
                alt = _safe_float(parts[5], None) if len(parts) > 5 else None
                speed = _safe_float(parts[6], None) if len(parts) > 6 else None
                course = _safe_float(parts[7], None) if len(parts) > 7 else None
                sats = _safe_int(parts[14], None) if len(parts) > 14 else None
                info = {
                    "run_status": run_status,
                    "fix_status": fix_status,
                    "utc": utc or None,
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": alt,
                    "speed": speed,
                    "course": course,
                    "satellites": sats,
                    "raw": body,
                }
                if self._gnss_has_location(info):
                    self._last_gnss_fix = info
                return info
        except Exception:
            pass
        return None

    def _gnss_has_location(self, info):
        if not isinstance(info, dict):
            return False
        lat = info.get("latitude")
        lon = info.get("longitude")
        if lat is None or lon is None:
            return False
        if not (-90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0):
            return False
        if abs(float(lat)) < 0.000001 and abs(float(lon)) < 0.000001:
            return False
        return True

    def ensure_gnss_power(self, enabled=True):
        want = 1 if enabled else 0
        ok_q, r_q = self.hal.send_at("+CGNSPWR?", "OK", 2000)
        if ok_q:
            for ln in (r_q or "").splitlines():
                if "+CGNSPWR:" in ln:
                    state = _safe_int(ln.split(":", 1)[1].strip(), None)
                    if state == want:
                        return True
        ok_set, _ = self.hal.send_at("+CGNSPWR={}".format(want), "OK", 5000)
        return bool(ok_set)

    def read_gnss_info(self):
        ok, resp = self.hal.send_at("+CGNSINF", "OK", 3000)
        if not ok:
            return None
        return self._parse_cgnsinf(resp or "")

    def read_gnss_location(self, ensure_power=True, attempts=1, delay_ms=1000):
        if ensure_power and not self.ensure_gnss_power(True):
            return self._last_gnss_fix
        tries = max(1, _safe_int(attempts, 1) or 1)
        for idx in range(tries):
            info = self.read_gnss_info()
            if self._gnss_has_location(info):
                return info
            if idx + 1 < tries:
                self.hal.sleep_ms(max(0, _safe_int(delay_ms, 1000) or 1000))
        return self._last_gnss_fix

    def _cnact_context_active(self, s, cid=0):
        try:
            for ln in s.splitlines():
                if "+CNACT:" not in ln:
                    continue
                body = ln.split(":", 1)[1].strip()
                parts = [p.strip().strip('"') for p in body.split(",")]
                if len(parts) < 3:
                    continue
                row_cid = int(parts[0])
                state = int(parts[1])
                ip = parts[2]
                if row_cid == int(cid) and state == 1 and ip and ip != "0.0.0.0":
                    return True
        except Exception:
            pass
        return False

    def _read_until_token_bytes(self, token, timeout_ms, grace_ms=0):
        token = token if isinstance(token, bytes) else token.encode()
        buf = b""; t0 = self.hal.ticks_ms()
        while self.hal.ticks_diff(self.hal.ticks_ms(), t0) < timeout_ms:
            chunk = self.hal.uart_read_some(256)
            if chunk:
                buf += chunk
                if token in buf:
                    if grace_ms:
                        self.hal.sleep_ms(grace_ms)
                        t1 = self.hal.ticks_ms()
                        while self.hal.ticks_diff(self.hal.ticks_ms(), t1) < 50:
                            more = self.hal.uart_read_some(256)
                            if more:
                                buf += more
                            else:
                                break
                    return True, buf
            else:
                self.hal.sleep_ms(5)
        return False, buf

    def _read_urc_regex(self, regex_pat, timeout_ms=60000, first_token=b"+"):
        ok, s = self._read_until_token_bytes(first_token, timeout_ms=timeout_ms, grace_ms=150)
        if not ok:
            return False, None, ""
        text = s.decode("utf-8", "ignore")
        mobj = re.search(regex_pat, text)
        if mobj:
            return True, mobj, text
        extra = b""; t1 = self.hal.ticks_ms()
        while self.hal.ticks_diff(self.hal.ticks_ms(), t1) < 200:
            more = self.hal.uart_read_some(128)
            if not more:
                break
            extra += more
        text2 = text + extra.decode("utf-8", "ignore")
        mobj2 = re.search(regex_pat, text2)
        return (mobj2 is not None), mobj2, text2

    # Envío crudo de AT sin leer respuesta (evita drenar UART antes del URC)
    def _drain_uart(self, timeout_ms=150):
        buf = b""
        t0 = self.hal.ticks_ms()
        while self.hal.ticks_diff(self.hal.ticks_ms(), t0) < timeout_ms:
            chunk = self.hal.uart_read_some(256)
            if chunk:
                buf += chunk
            else:
                self.hal.sleep_ms(20)
        return buf

    def _sync_at(self, tries=3, timeout_ms=1200, gap_ms=150):
        self._drain_uart(150)
        for _ in range(max(1, int(tries))):
            ok, _ = self.hal.send_at("AT", "OK", timeout_ms)
            if ok:
                self.hal.send_at("ATE0", "OK", 1000)
                self.hal.send_at("+CMEE=2", "OK", 1000)
                return True
            self.hal.sleep_ms(gap_ms)
        return False

    def _write_at_noresp(self, cmd: str):
        if not str(cmd).startswith("AT"):
            cmd = "AT" + str(cmd)
        _log_debug(self.debug >= 1, ">>", cmd)
        self.hal.write_raw((cmd + "\r\n").encode())
        return True

    # --------- start / radio ----------
    def start(self):
        _log_debug(self.debug, "[0] Arranque")
        if self._sync_at(tries=3, timeout_ms=1200, gap_ms=200):
            return True
        self.hal.pwr_pulse()
        self.hal.sleep(5)
        if self._sync_at(tries=4, timeout_ms=1500, gap_ms=250):
            return True
        return self._sync_at(tries=3, timeout_ms=1500, gap_ms=250)

    def set_radio_nbiot(self):
        _log_debug(self.debug, f"[1] NB-IoT B{self.nb_band}")
        self.hal.send_at("+CNMP=38", "OK", 1200)
        self.hal.send_at("+CMNB=2", "OK", 1200)
        self.hal.send_at('+CBANDCFG="NB-IOT",{}'.format(self.nb_band), "OK", 2500)
        self.hal.send_at("+CFUN=1", "OK", 3500); self.hal.sleep(3)

    def _cereg_ok(self):
        ok, r = self.hal.send_at("+CEREG?", "OK", 1200)
        if not ok:
            return False
        for ln in r.splitlines():
            if "+CEREG:" in ln:
                try:
                    stat = int(ln.split(":")[1].split(",")[1].strip())
                    return stat in (1, 5)
                except Exception:
                    pass
        return False

    def attach_and_pdp(self, fallback_apn="m2m.entel.cl", wait_ms=120000):
        _log_debug(self.debug, "[2] PDP/APN")
        ok_pin, _ = self.hal.send_at("+CPIN?", "READY", 1500)
        if not ok_pin:
            return False
        self.hal.send_at("+CSQ", "OK", 1200)
        ok, r = self.hal.send_at("+CGNAPN", "OK", 2500)
        _, apn = self._parse_cgnapn(r or "")
        if not apn:
            apn = fallback_apn
        ok_cfg, _ = self.hal.send_at('+CNCFG=0,1,"{}"'.format(apn), "OK", 1500)
        if not ok_cfg:
            return False

        registered = False
        t0 = self.hal.ticks_ms()
        while self.hal.ticks_diff(self.hal.ticks_ms(), t0) < wait_ms:
            if self._cereg_ok():
                registered = True
                break
            self.hal.sleep(2)
        if not registered:
            return False

        ok_act, _ = self.hal.send_at("+CNACT=0,1", "OK", 60000)
        ok_q, r_q = self.hal.send_at("+CNACT?", "OK", 1500)
        pdp_active = self._cnact_context_active(r_q or "", cid=0)
        # PDN de sockets = 0. En algunos firmwares devuelve ERROR aunque el
        # contexto 0 ya esté activo y sea el usado por defecto.
        ok_sock, r_sock = self.hal.send_at("+CSOCKSETPN=0", "OK", 1500)
        if not ok_sock and self.debug:
            _log_debug(True, "[pdp] CSOCKSETPN no confirmado, continuando con contexto activo:", repr(r_sock or ""))
        return bool((ok_act or pdp_active) and ok_q)

    # --------- DNS helper ----------
    def _dns_resolve_ip(self, host, timeout_ms=60000):
        _log_debug(self.debug, "[dns] CDNSGIP ->", host)
        _, resp = self.hal.send_at('+CDNSGIP="{}"'.format(host), "OK", timeout_ms)
        dump = resp or ""
        ip = None

        def _extract_ip(text):
            for ln in (text or "").splitlines():
                if "+CDNSGIP:" not in ln:
                    continue
                q = []
                in_quote = False
                cur = ""
                for ch in ln:
                    if ch == '"':
                        if in_quote:
                            q.append(cur)
                            cur = ""
                            in_quote = False
                        else:
                            in_quote = True
                        continue
                    if in_quote:
                        cur += ch
                for part in q:
                    pieces = part.split(".")
                    if len(pieces) != 4:
                        continue
                    ok_ip = True
                    for piece in pieces:
                        if not piece.isdigit():
                            ok_ip = False
                            break
                        value = int(piece)
                        if value < 0 or value > 255:
                            ok_ip = False
                            break
                    if ok_ip:
                        return part
            return None

        ip = _extract_ip(dump)
        if not ip:
            ok, _, late_dump = self._read_urc_regex(
                r"\+CDNSGIP:",
                timeout_ms=max(1000, timeout_ms // 2),
                first_token=b"+"
            )
            if ok and late_dump:
                dump = dump + late_dump
                ip = _extract_ip(dump)
        _log_debug(self.debug, "[dns] IP =", ip or "(no resuelto)")
        return ip

    # --------- TLS / sockets ----------
    def _tls_basic_setup(self, host, force_cipher=None):
        # force_cipher se conserva en la firma por compatibilidad interna.
        _log_debug(
            self.debug,
            "[tls] CTX={}, SNI={}, ciphers={}".format(
                self.TLS_CTX,
                host,
                self.TLS_CIPHERS,
            ),
        )
        self.hal.send_at('+CSSLCFG="SSLVERSION",{},3'.format(self.TLS_CTX), "OK", 2000)
        self.hal.send_at('+CSSLCFG="IGNORERTCTIME",{},1'.format(self.TLS_CTX), "OK", 2000)
        self.hal.send_at('+CSSLCFG="PROTOCOL",{},1'.format(self.TLS_CTX), "OK", 2000)
        self.hal.send_at('+CSSLCFG="SNI",{},"{}"'.format(self.TLS_CTX, host), "OK", 2000)
        for index, cipher in enumerate(self.TLS_CIPHERS):
            self.hal.send_at(
                '+CSSLCFG="CIPHERSUITE",{},{},{}'.format(
                    self.TLS_CTX,
                    index,
                    cipher,
                ),
                "OK",
                2000,
            )
        self.hal.send_at('+CSSLCFG="CTXINDEX",{}'.format(self.TLS_CTX), "OK", 2000)

    def _bind_tls_socket(self):
        ok_ctx, _ = self.hal.send_at(
            '+CASSLCFG={},"CRINDEX",{}'.format(self.SOCK_ID, self.TLS_CTX),
            "OK",
            2000,
        )
        ok_ssl, _ = self.hal.send_at(
            '+CASSLCFG={},"SSL",1'.format(self.SOCK_ID),
            "OK",
            2000,
        )
        return bool(ok_ctx and ok_ssl)

    def _explain_caopen_err(self, code: int) -> str:
        table = {
            1: "socket error",
            2: "sin memoria",
            3: "limite de conexiones",
            4: "parametro invalido",
            6: "direccion IP invalida",
            7: "funcion no soportada",
            12: "no se pudo enlazar el puerto",
            13: "no se pudo escuchar el puerto",
            20: "no se pudo resolver el host",
            21: "red no activa",
            23: "conexion rechazada por remoto",
            24: "certificado expirado",
            25: "nombre comun del certificado no coincide",
            26: "certificado expirado y nombre no coincide",
            27: "fallo de conexion TCP/TLS",
        }
        return table.get(code, "error desconocido")

    def _ensure_socket_context_ready(self, settle_ms=2500):
        self.hal.sleep_ms(max(0, int(settle_ms)))
        ok_q, r_q = self.hal.send_at("+CNACT?", "OK", 1500)
        if self._cnact_context_active(r_q or "", cid=0):
            return True
        self.hal.send_at("+CNACT=0,1", "OK", 15000)
        self.hal.sleep_ms(1000)
        ok_q2, r_q2 = self.hal.send_at("+CNACT?", "OK", 1500)
        return self._cnact_context_active(r_q2 or "", cid=0)

    def socket_open(self, host, port=443, timeout_ms=90000, connect_host=None):
        self._ensure_cid()

        # Cierra por si quedó algo colgado
        self.hal.send_at("+CACLOSE={}".format(self.SOCK_ID), None, 200)

        # Setup TLS (contexto)
        self._tls_basic_setup(host, force_cipher=None)

        # Enlaza explicitamente el socket CID con el contexto TLS.
        if not self._bind_tls_socket():
            _log_debug(self.debug, "[tls] no fue posible enlazar socket y contexto")
            return False

        # Resolver DNS y abrir por IP (manteniendo SNI=host en el CTX).
        # connect_host permite bypassear DNS del modem sin alterar Host/SNI.
        ip = connect_host or self._dns_resolve_ip(host, timeout_ms=45000)
        target = connect_host or host
        _log_debug(self.debug, "[sock] CAOPEN CID={}, target={}, port={}".format(self.SOCK_ID, target, port))
        self._write_at_noresp('+CAOPEN={},0,"TCP","{}",{}'.format(self.SOCK_ID, target, port))

        ok, mobj, dump1 = self._read_urc_regex(
            r"\+CAOPEN:\s*(\d+)\s*,\s*(\d+)",
            timeout_ms=timeout_ms,
            first_token=b"+"
        )
        if ok:
            cid = int(mobj.group(1)); rc = int(mobj.group(2))
            _log_debug(self.debug, "[sock] +CAOPEN:", cid, rc, ("OK" if rc==0 else self._explain_caopen_err(rc)))
            if cid == self.SOCK_ID and rc == 0:
                return True
            if cid == self.SOCK_ID and rc == 27:
                _log_debug(self.debug, "[sock] rc=27; esperando estabilizacion PDP y reintentando")
                self.hal.send_at("+CACLOSE={}".format(self.SOCK_ID), None, 200)
                self._ensure_socket_context_ready(settle_ms=3000)
                _log_debug(self.debug, "[sock] CAOPEN post-settle CID={}, target={}, port={}".format(self.SOCK_ID, target, port))
                self._write_at_noresp('+CAOPEN={},0,"TCP","{}",{}'.format(self.SOCK_ID, target, port))
                ok1b, mobj1b, _ = self._read_urc_regex(
                    r"\+CAOPEN:\s*(\d+)\s*,\s*(\d+)",
                    timeout_ms=timeout_ms,
                    first_token=b"+"
                )
                if ok1b:
                    cid1b = int(mobj1b.group(1)); rc1b = int(mobj1b.group(2))
                    _log_debug(self.debug, "[sock] +CAOPEN post-settle:", cid1b, rc1b, ("OK" if rc1b==0 else self._explain_caopen_err(rc1b)))
                    if cid1b == self.SOCK_ID and rc1b == 0:
                        return True

        # Reintento con ECDHE
        self.hal.send_at("+CACLOSE={}".format(self.SOCK_ID), None, 200)
        self._tls_basic_setup(host, force_cipher=None)
        if not self._bind_tls_socket():
            _log_debug(self.debug, "[tls] no fue posible reenlazar socket y contexto")
            return False
        if not ip and not connect_host:
            ip = self._dns_resolve_ip(host, timeout_ms=45000)
        target = connect_host or host
        _log_debug(self.debug, "[sock] CAOPEN (retry ECDHE) CID={}, target={}, port={}".format(self.SOCK_ID, target, port))
        self._write_at_noresp('+CAOPEN={},0,"TCP","{}",{}'.format(self.SOCK_ID, target, port))

        ok2, mobj2, dump2 = self._read_urc_regex(
            r"\+CAOPEN:\s*(\d+)\s*,\s*(\d+)",
            timeout_ms=timeout_ms,
            first_token=b"+"
        )
        if ok2:
            cid2 = int(mobj2.group(1)); rc2 = int(mobj2.group(2))
            _log_debug(self.debug, "[sock] +CAOPEN (retry):", cid2, rc2, ("OK" if rc2==0 else self._explain_caopen_err(rc2)))
            if cid2 == self.SOCK_ID and rc2 == 0:
                return True
            if cid2 == self.SOCK_ID and rc2 == 27:
                _log_debug(self.debug, "[sock] rc=27 en retry; esperando estabilizacion PDP y reintentando")
                self.hal.send_at("+CACLOSE={}".format(self.SOCK_ID), None, 200)
                self._ensure_socket_context_ready(settle_ms=3000)
                _log_debug(self.debug, "[sock] CAOPEN retry post-settle CID={}, target={}, port={}".format(self.SOCK_ID, target, port))
                self._write_at_noresp('+CAOPEN={},0,"TCP","{}",{}'.format(self.SOCK_ID, target, port))
                ok2b, mobj2b, _ = self._read_urc_regex(
                    r"\+CAOPEN:\s*(\d+)\s*,\s*(\d+)",
                    timeout_ms=timeout_ms,
                    first_token=b"+"
                )
                if ok2b:
                    cid2b = int(mobj2b.group(1)); rc2b = int(mobj2b.group(2))
                    _log_debug(self.debug, "[sock] +CAOPEN retry post-settle:", cid2b, rc2b, ("OK" if rc2b==0 else self._explain_caopen_err(rc2b)))
                    if cid2b == self.SOCK_ID and rc2b == 0:
                        return True

        # Último recurso: CASTATE?
        _log_debug(self.debug, "[sock] Revisando CASTATE? (último recurso)")
        t1 = self.hal.ticks_ms()
        while self.hal.ticks_diff(self.hal.ticks_ms(), t1) < timeout_ms:
            ok3, r = self.hal.send_at("+CASTATE?", "OK", 1200)
            if ok3:
                for ln in r.splitlines():
                    if "+CASTATE:" in ln:
                        try:
                            cid = int(ln.split(":")[1].split(",")[0].strip())
                            st  = int(ln.split(":")[1].split(",")[1].strip())
                            _log_debug(self.debug, "[sock] CASTATE:", cid, st)
                            if cid == self.SOCK_ID and st == 1:
                                return True
                        except Exception:
                            pass
            self.hal.sleep_ms(150)
        _log_debug(self.debug, "[sock] No se pudo abrir socket TLS")
        return False

    def socket_close(self):
        self.hal.send_at("+CACLOSE={}".format(self.SOCK_ID), "OK", 4000)

    def _wait_for_ok(self, timeout_ms=1500):
        """Espera SOLO el terminador '\r\nOK\r\n' sin chupar datos demás."""
        tail = b""
        t0 = self.hal.ticks_ms()
        while self.hal.ticks_diff(self.hal.ticks_ms(), t0) < timeout_ms:
            chunk = self.hal.uart_read_some(256)
            if chunk:
                tail = (tail + chunk)[-8:]  # guarda solo la cola
                if b"\r\nOK\r\n" in tail:
                    return True
            else:
                self.hal.sleep_ms(5)
        return False

    def socket_send(self, payload, wait_ok_ms=2000):
        self._ensure_cid()
        data = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
        ok, _ = self.hal.send_at("+CASEND={},{}".format(self.SOCK_ID, len(data)), ">", 4000)
        if not ok:
            return False
        self.hal.write_raw(data)
        _ = self._wait_for_ok(timeout_ms=wait_ok_ms)
        return True

    def carecv_once_exact(self, ask_len, overall_ms=8000):
        self._ensure_cid()
        return self.hal.carecv_once_exact(ask_len, overall_ms)

    # --------- HTTP ----------
    def _build_headers(self, host, user_agent, extra=None):
        base = {
            "Host": host,
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Connection": "close",
        }
        if extra:
            base.update(extra)
        return "".join("{}: {}\r\n".format(k, v) for k, v in base.items())

    def _decode_chunked(self, body_bytes: bytes) -> bytes:
        out = b""; i = 0; L = len(body_bytes)
        while True:
            j = body_bytes.find(b"\r\n", i)
            if j < 0:
                break
            size_line = body_bytes[i:j].split(b";", 1)[0].strip()
            try:
                size = int(size_line, 16)
            except Exception:
                break
            i = j + 2
            if size == 0:
                return out
            if i + size + 2 > L:
                break
            out += body_bytes[i:i + size]; i += size + 2
        return out

    def http_read_response_exact(self):
        acc = b""; headers_done = False; header_end = -1; delim_len = 0
        headers_map = {}; content_len = None; is_chunked = False

        t0 = self.hal.ticks_ms()
        while True:
            chunk = self.carecv_once_exact(1460, overall_ms=4000)
            if not chunk:
                if self.hal.ticks_diff(self.hal.ticks_ms(), t0) > self.READ_TIMEOUT:
                    break
                self.hal.sleep_ms(50); continue
            acc += chunk
            txt = acc.decode("utf-8", "ignore")
            if not headers_done:
                idx = txt.find("\r\n\r\n")
                if idx >= 0:
                    header_end = idx; delim_len = 4; headers_done = True
                else:
                    idx2 = txt.find("\n\n")
                    if idx2 >= 0:
                        header_end = idx2; delim_len = 2; headers_done = True
            if headers_done:
                break
            if self.hal.ticks_diff(self.hal.ticks_ms(), t0) > self.READ_TIMEOUT:
                break

        if header_end < 0:
            txt = acc.decode("utf-8", "ignore"); status = 0
            parts = txt.split()
            if len(parts) >= 2 and parts[0].startswith("HTTP/"):
                try:
                    status = int(parts[1])
                except Exception:
                    status = 0
            return status, {}, txt, acc

        headers_text = acc[:header_end].decode("utf-8", "ignore")
        for ln in headers_text.splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1); headers_map[k.strip().lower()] = v.strip()

        status = 0
        first_line = headers_text.split("\r\n", 1)[0]
        parts = first_line.split()
        if len(parts) >= 2 and parts[0].startswith("HTTP/"):
            try:
                status = int(parts[1])
            except Exception:
                status = 0

        if "content-length" in headers_map:
            try:
                content_len = int(headers_map["content-length"])
            except Exception:
                content_len = None

        te = headers_map.get("transfer-encoding", "").lower()
        if "chunked" in te:
            is_chunked = True

        body = acc[header_end + delim_len:]

        if content_len is not None and not is_chunked:
            if len(body) < content_len:
                need = content_len - len(body); t1 = self.hal.ticks_ms()
                while need > 0 and self.hal.ticks_diff(self.hal.ticks_ms(), t1) < (self.READ_TIMEOUT * 2):
                    chunk = self.carecv_once_exact(min(1460, need), overall_ms=4000)
                    if not chunk:
                        self.hal.sleep_ms(40); continue
                    body += chunk; need = content_len - len(body)
        elif is_chunked:
            end_marker = b"\r\n0\r\n\r\n"; t1 = self.hal.ticks_ms()
            while end_marker not in body and self.hal.ticks_diff(self.hal.ticks_ms(), t1) < (self.READ_TIMEOUT * 2):
                chunk = self.carecv_once_exact(1460, overall_ms=1500)
                if not chunk:
                    self.hal.sleep_ms(40); continue
                body += chunk
            body = self._decode_chunked(body)
        else:
            idle_reads = 0
            while idle_reads < 8:
                chunk = self.carecv_once_exact(1460, overall_ms=1500)
                if not chunk:
                    idle_reads += 1
                    self.hal.sleep_ms(200)
                    continue
                idle_reads = 0
                body += chunk

        if content_len is not None and len(body) > content_len:
            body = body[:content_len]

        try:
            body_txt = body.decode("utf-8", "ignore")
        except Exception:
            body_txt = ""

        return status, headers_map, body_txt, acc

    def http_post_json_return(self, host, port, user_agent, path, body_dict, extra_headers=None, connect_host=None):
        body = json.dumps(body_dict)
        hdrs = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if extra_headers:
            hdrs.update(extra_headers)
        header_str = self._build_headers(host, user_agent, hdrs)
        req = "POST {} HTTP/1.0\r\n{}\r\n{}".format(path, header_str, body)

        if not self.socket_open(host, port, connect_host=connect_host):
            return 0, None, ""
        if not self.socket_send(req):
            self.socket_close(); return 0, None, ""

        status, headers, body_txt, raw = self.http_read_response_exact()
        self.socket_close()

        obj = None
        if body_txt:
            try:
                obj = json.loads(body_txt)
            except Exception:
                obj = None
        return status, obj, body_txt

    def http_get_json_return(self, host, port, user_agent, path, extra_headers=None, connect_host=None):
        hdrs = {}
        if extra_headers:
            hdrs.update(extra_headers)
        header_str = self._build_headers(host, user_agent, hdrs)
        req = "GET {} HTTP/1.0\r\n{}\r\n".format(path, header_str)

        if not self.socket_open(host, port, connect_host=connect_host):
            return 0, None, ""
        if not self.socket_send(req):
            self.socket_close()
            return 0, None, ""

        status, headers, body_txt, raw = self.http_read_response_exact()
        self.socket_close()

        obj = None
        if body_txt:
            try:
                obj = json.loads(body_txt)
            except Exception:
                obj = None
        return status, obj, body_txt

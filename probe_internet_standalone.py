"""
Probe autocontenido de Internet para Raspberry Pi Pico + SIM7080.

No importa archivos del proyecto ni lee device_config.json. Solo requiere
MicroPython con machine y utime.
"""

try:
    import machine
    import utime as time
except Exception as exc:
    raise RuntimeError("Ejecutar este archivo en la Pico con MicroPython") from exc

try:
    import ujson as json
except Exception:
    json = None


# Hardware conocido de la placa.
UART_ID = 0
UART_TX_PIN = 0
UART_RX_PIN = 1
UART_BAUD = 115200
PWR_EN_PIN = 14

# Red y prueba HTTP.
NB_BAND = 28
APN = "m2m.entel.cl"
TEST_HOST = "jsonplaceholder.typicode.com"
TEST_PATH = "/todos/1"
USER_AGENT = "pico-sim7080-standalone-probe/1.0"
TLS_CIPHERS = ("0xC02B", "0xC02C")

REGISTRATION_TIMEOUT_MS = 120000
DNS_TIMEOUT_MS = 45000
SOCKET_TIMEOUT_MS = 60000
HTTP_READ_TIMEOUT_MS = 20000
DEBUG = 2


class StandaloneSIM7080:
    def __init__(self):
        self.uart = machine.UART(
            UART_ID,
            baudrate=UART_BAUD,
            tx=machine.Pin(UART_TX_PIN),
            rx=machine.Pin(UART_RX_PIN),
        )
        self.pwr = machine.Pin(PWR_EN_PIN, machine.Pin.OUT)
        self.pwr.value(0)
        self.sock_id = 0
        self.tls_ctx = 0

    def _log(self, *args):
        if DEBUG:
            print(*args)

    def _read_uart(self, max_bytes=512):
        try:
            if self.uart.any():
                return self.uart.read(max_bytes) or b""
        except Exception:
            pass
        return b""

    def _drain(self, duration_ms=150):
        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < duration_ms:
            if not self._read_uart():
                time.sleep_ms(10)

    def _print_response(self, data):
        if DEBUG < 2 or not data:
            return
        text = data.decode("utf-8", "ignore")
        for line in text.splitlines():
            print("<<", line)

    def _read_until(self, tokens, timeout_ms, grace_ms=80):
        if not isinstance(tokens, (tuple, list)):
            tokens = (tokens,)
        wanted = []
        for token in tokens:
            wanted.append(token if isinstance(token, bytes) else token.encode())

        buf = b""
        start = time.ticks_ms()
        found = None

        while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
            chunk = self._read_uart()
            if chunk:
                buf += chunk
                for token in wanted:
                    if token in buf:
                        found = token
                        break
                if found is not None:
                    break
            else:
                time.sleep_ms(10)

        if found is not None and grace_ms > 0:
            end = time.ticks_ms()
            while time.ticks_diff(time.ticks_ms(), end) < grace_ms:
                chunk = self._read_uart()
                if chunk:
                    buf += chunk
                    end = time.ticks_ms()
                else:
                    time.sleep_ms(10)

        return found, buf

    def command(self, cmd, expect="OK", timeout_ms=2000):
        if not cmd.startswith("AT"):
            cmd = "AT" + cmd
        self._drain()
        print(">>", cmd)
        self.uart.write((cmd + "\r\n").encode())

        tokens = [expect, "ERROR", "+CME ERROR"] if expect else ["OK", "ERROR", "+CME ERROR"]
        found, data = self._read_until(tokens, timeout_ms)
        self._print_response(data)

        if expect is None:
            return found is not None, data.decode("utf-8", "ignore")
        ok = found is not None and expect.encode() in data
        return ok, data.decode("utf-8", "ignore")

    def power_pulse(self):
        print("[modem] pulso PWR_EN")
        self.pwr.value(1)
        time.sleep_ms(1800)
        self.pwr.value(0)
        time.sleep(5)

    def start(self):
        print("[1] Sincronizacion AT")
        for _ in range(3):
            ok, _ = self.command("AT", "OK", 1200)
            if ok:
                self.command("ATE0", "OK", 1200)
                self.command("AT+CMEE=2", "OK", 1200)
                return True
            time.sleep_ms(250)

        self.power_pulse()
        for _ in range(4):
            ok, _ = self.command("AT", "OK", 1500)
            if ok:
                self.command("ATE0", "OK", 1200)
                self.command("AT+CMEE=2", "OK", 1200)
                return True
            time.sleep_ms(300)
        return False

    def configure_radio(self):
        print("[2] Configuracion NB-IoT")
        self.command("AT+CGNSPWR=0", "OK", 3000)
        self.command("AT+CNMP=38", "OK", 2000)
        self.command("AT+CMNB=2", "OK", 2000)
        self.command('AT+CBANDCFG="NB-IOT",{}'.format(NB_BAND), "OK", 3000)
        self.command("AT+CFUN=1", "OK", 5000)
        time.sleep(3)

    def _registered(self, response):
        for line in (response or "").splitlines():
            if "+CEREG:" not in line:
                continue
            try:
                fields = line.split(":", 1)[1].split(",")
                status = int(fields[1].strip())
                return status in (1, 5)
            except Exception:
                pass
        return False

    def _pdp_active(self, response):
        for line in (response or "").splitlines():
            if "+CNACT:" not in line:
                continue
            try:
                fields = line.split(":", 1)[1].split(",")
                cid = int(fields[0].strip())
                active = int(fields[1].strip())
                ip = fields[2].strip().strip('"')
                if cid == 0 and active == 1 and ip and ip != "0.0.0.0":
                    return True, ip
            except Exception:
                pass
        return False, None

    def attach_network(self):
        print("[3] Registro y contexto PDP")
        ok, _ = self.command("AT+CPIN?", "READY", 2500)
        if not ok:
            print("[FAIL] SIM no disponible")
            return False

        self.command("AT+CSQ", "OK", 2000)
        self.command("AT+CGATT?", "OK", 2000)
        self.command("AT+COPS?", "OK", 3000)
        self.command("AT+CGNAPN", "OK", 3000)

        ok, _ = self.command('AT+CNCFG=0,1,"{}"'.format(APN), "OK", 3000)
        if not ok:
            print("[FAIL] No fue posible configurar APN")
            return False

        start = time.ticks_ms()
        registered = False
        while time.ticks_diff(time.ticks_ms(), start) < REGISTRATION_TIMEOUT_MS:
            ok, response = self.command("AT+CEREG?", "OK", 2000)
            if ok and self._registered(response):
                registered = True
                break
            time.sleep(2)

        if not registered:
            print("[FAIL] Sin registro NB-IoT")
            return False

        # Algunos firmwares responden ERROR si el contexto ya estaba activo.
        self.command("AT+CNACT=0,1", "OK", 60000)
        ok, response = self.command("AT+CNACT?", "OK", 2500)
        active, ip = self._pdp_active(response)
        if not ok or not active:
            print("[FAIL] Contexto PDP 0 inactivo")
            return False

        print("[OK] PDP activo, IP local =", ip)

        ok_sock, response_sock = self.command("AT+CSOCKSETPN=0", "OK", 2500)
        if not ok_sock:
            print("[WARN] CSOCKSETPN no soportado:", repr(response_sock))

        # Evita abrir el socket inmediatamente despues de activar el contexto.
        time.sleep(3)
        return True

    def resolve_dns(self, host):
        print("[4] Resolucion DNS")
        self._drain()
        cmd = 'AT+CDNSGIP="{}"'.format(host)
        print(">>", cmd)
        self.uart.write((cmd + "\r\n").encode())
        _, data = self._read_until(("+CDNSGIP:", "ERROR", "+CME ERROR"), DNS_TIMEOUT_MS, 250)
        self._print_response(data)
        text = data.decode("utf-8", "ignore")

        for line in text.splitlines():
            if "+CDNSGIP:" not in line:
                continue
            quoted = []
            current = ""
            inside = False
            for char in line:
                if char == '"':
                    if inside:
                        quoted.append(current)
                        current = ""
                    inside = not inside
                elif inside:
                    current += char
            for value in quoted:
                parts = value.split(".")
                if len(parts) != 4:
                    continue
                try:
                    if all(0 <= int(part) <= 255 for part in parts):
                        print("[OK] DNS =", value)
                        return value
                except Exception:
                    pass

        print("[FAIL] DNS no entrego una direccion IPv4")
        return None

    def _tls_setup(self, host):
        print("[tls] configurando SNI =", host, "ciphers =", TLS_CIPHERS)
        self.command('AT+CSSLCFG="SSLVERSION",0,3', "OK", 2500)
        self.command('AT+CSSLCFG="IGNORERTCTIME",0,1', "OK", 2500)
        self.command('AT+CSSLCFG="PROTOCOL",0,1', "OK", 2500)
        self.command('AT+CSSLCFG="SNI",0,"{}"'.format(host), "OK", 2500)
        for index, cipher in enumerate(TLS_CIPHERS):
            self.command(
                'AT+CSSLCFG="CIPHERSUITE",0,{},{}'.format(index, cipher),
                "OK",
                2500,
            )
        self.command('AT+CSSLCFG="CTXINDEX",0', "OK", 2500)

    def _parse_caopen(self, text):
        for line in (text or "").splitlines():
            if "+CAOPEN:" not in line:
                continue
            try:
                fields = line.split(":", 1)[1].split(",")
                return int(fields[0].strip()), int(fields[1].strip())
            except Exception:
                pass
        return None, None

    def socket_close(self):
        self.command("AT+CACLOSE={}".format(self.sock_id), None, 2500)

    def socket_open(self, host, target, port, use_tls):
        self.socket_close()

        if use_tls:
            self._tls_setup(host)
            # CRINDEX enlaza el socket CID con el contexto configurado por
            # CSSLCFG. Activar "SSL" por si solo no garantiza ese enlace.
            self.command(
                'AT+CASSLCFG={},"CRINDEX",{}'.format(self.sock_id, self.tls_ctx),
                "OK",
                2500,
            )
            self.command('AT+CASSLCFG={},"SSL",1'.format(self.sock_id), "OK", 2500)
            self.command("AT+CASSLCFG?", "OK", 2500)
        else:
            self.command('AT+CASSLCFG={},"SSL",0'.format(self.sock_id), "OK", 2500)

        self._drain()
        cmd = 'AT+CAOPEN={},0,"TCP","{}",{}'.format(self.sock_id, target, port)
        print(">>", cmd)
        self.uart.write((cmd + "\r\n").encode())
        _, data = self._read_until(("+CAOPEN:", "ERROR", "+CME ERROR"), SOCKET_TIMEOUT_MS, 200)
        self._print_response(data)

        cid, result = self._parse_caopen(data.decode("utf-8", "ignore"))
        if cid == self.sock_id and result in (0, 2):
            print("[OK] Socket abierto")
            return True, result

        print("[FAIL] CAOPEN cid =", cid, "resultado =", result)
        return False, result

    def socket_send(self, payload):
        data = payload if isinstance(payload, bytes) else payload.encode()
        ok, _ = self.command(
            "AT+CASEND={},{}".format(self.sock_id, len(data)),
            ">",
            5000,
        )
        if not ok:
            print("[FAIL] El modem no entrego prompt CASEND")
            return False

        print(">> [HTTP payload {} bytes]".format(len(data)))
        self.uart.write(data)
        found, response = self._read_until(("OK", "ERROR", "+CME ERROR"), 8000)
        self._print_response(response)
        return found == b"OK"

    def _carecv_once(self, ask_len=1460, timeout_ms=5000):
        self._drain(50)
        cmd = "AT+CARECV={},{}\r\n".format(self.sock_id, int(ask_len))
        print(">>", cmd.strip())
        self.uart.write(cmd.encode())

        buf = b""
        start = time.ticks_ms()
        marker = b"+CARECV:"

        while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
            chunk = self._read_uart()
            if chunk:
                buf += chunk
                marker_pos = buf.find(marker)
                if marker_pos < 0:
                    if b"ERROR" in buf:
                        self._print_response(buf)
                        return b""
                    continue

                comma_pos = buf.find(b",", marker_pos)
                if comma_pos < 0:
                    continue

                colon_pos = buf.find(b":", marker_pos)
                try:
                    size = int(buf[colon_pos + 1:comma_pos].strip())
                except Exception:
                    continue

                data_start = comma_pos + 1
                if len(buf) - data_start >= size:
                    payload = buf[data_start:data_start + size]
                    if DEBUG >= 2 and payload:
                        print("<< [socket data {} bytes]".format(len(payload)))
                    return payload
            else:
                time.sleep_ms(15)

        return b""

    def read_http_response(self):
        received = b""
        start = time.ticks_ms()
        idle = 0

        while time.ticks_diff(time.ticks_ms(), start) < HTTP_READ_TIMEOUT_MS:
            chunk = self._carecv_once()
            if chunk:
                received += chunk
                idle = 0
                if b"\r\n0\r\n\r\n" in received:
                    break
                if b"\r\n\r\n" in received:
                    header_end = received.find(b"\r\n\r\n")
                    header_text = received[:header_end].decode("utf-8", "ignore").lower()
                    marker = "content-length:"
                    if marker in header_text:
                        try:
                            length_text = header_text.split(marker, 1)[1].splitlines()[0].strip()
                            content_length = int(length_text)
                            if len(received) - header_end - 4 >= content_length:
                                break
                        except Exception:
                            pass
            else:
                idle += 1
                if received and idle >= 2:
                    break
                time.sleep_ms(100)

        return received

    def _decode_chunked(self, body):
        output = b""
        pos = 0
        while pos < len(body):
            end = body.find(b"\r\n", pos)
            if end < 0:
                break
            try:
                size = int(body[pos:end].split(b";", 1)[0], 16)
            except Exception:
                break
            pos = end + 2
            if size == 0:
                break
            output += body[pos:pos + size]
            pos += size + 2
        return output

    def parse_http(self, raw):
        if not raw:
            return 0, "", ""

        split_at = raw.find(b"\r\n\r\n")
        if split_at < 0:
            text = raw.decode("utf-8", "ignore")
            return 0, "", text

        headers = raw[:split_at].decode("utf-8", "ignore")
        body = raw[split_at + 4:]
        status = 0
        first_line = headers.splitlines()[0] if headers else ""
        fields = first_line.split()
        if len(fields) >= 2:
            try:
                status = int(fields[1])
            except Exception:
                pass

        if "transfer-encoding: chunked" in headers.lower():
            body = self._decode_chunked(body)

        return status, headers, body.decode("utf-8", "ignore")

    def http_get(self, host, target, port, use_tls):
        scheme = "https" if use_tls else "http"
        print("")
        print("[TEST] {}://{}:{}{}".format(scheme, host, port, TEST_PATH))
        print("[TEST] CAOPEN target =", target)

        opened, result = self.socket_open(
            host=host,
            target=target,
            port=port,
            use_tls=use_tls,
        )
        if not opened:
            return False, result, 0, ""

        request = (
            "GET {} HTTP/1.0\r\n"
            "Host: {}\r\n"
            "User-Agent: {}\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).format(TEST_PATH, host, USER_AGENT)

        if not self.socket_send(request):
            self.socket_close()
            return False, result, 0, ""

        raw = self.read_http_response()
        self.socket_close()
        status, headers, body = self.parse_http(raw)

        print("[HTTP] status =", status)
        if headers:
            print(headers)
        if body:
            print("[HTTP] body =", body[:500])
            if json is not None:
                try:
                    print("[HTTP] json =", json.loads(body))
                except Exception:
                    pass

        return status > 0, result, status, body


def main():
    print("=== Probe Internet autocontenido SIM7080 ===")
    print("No usa sim7080mini, device_config.json, token, GPS ni WebSocket.")
    print(
        "UART{} TX=GP{} RX=GP{} baud={}".format(
            UART_ID,
            UART_TX_PIN,
            UART_RX_PIN,
            UART_BAUD,
        )
    )
    print("APN =", APN)
    print("")

    modem = StandaloneSIM7080()

    if not modem.start():
        print("[FINAL] FAIL_UART: el modem no responde")
        return

    modem.configure_radio()
    if not modem.attach_network():
        print("[FINAL] FAIL_PDP: sin acceso a la red movil")
        return

    resolved_ip = modem.resolve_dns(TEST_HOST)
    if not resolved_ip:
        print("[FINAL] FAIL_DNS: PDP activo pero DNS no responde")
        return

    # Caso principal: HTTPS usando hostname, igual que una URL normal.
    ok, ca_result, status, _ = modem.http_get(
        TEST_HOST,
        TEST_HOST,
        443,
        use_tls=True,
    )
    if ok:
        print("[FINAL] OK_HTTPS_HOST status =", status)
        return

    # Segundo caso: IP resuelta, conservando Host y SNI.
    ok, ca_result_ip, status, _ = modem.http_get(
        TEST_HOST,
        resolved_ip,
        443,
        use_tls=True,
    )
    if ok:
        print("[FINAL] OK_HTTPS_IP status =", status)
        return

    # Control TCP sin TLS. Un 200 o redireccion HTTP demuestra salida a Internet.
    ok_plain, ca_result_plain, status_plain, _ = modem.http_get(
        TEST_HOST,
        resolved_ip,
        80,
        use_tls=False,
    )
    if ok_plain:
        print("[FINAL] OK_TCP_HTTP status =", status_plain)
        print("[FINAL] Internet/TCP funciona; el problema esta aislado en TLS.")
        return

    print("")
    print("[FINAL] FAIL_SOCKET")
    print("HTTPS hostname CAOPEN =", ca_result)
    print("HTTPS IP CAOPEN =", ca_result_ip)
    print("HTTP puerto 80 CAOPEN =", ca_result_plain)
    print("PDP y DNS funcionan, pero el modem no logra abrir sockets TCP.")


try:
    main()
except KeyboardInterrupt:
    print("")
    print("[STOP] Prueba interrumpida.")
except Exception as exc:
    print("")
    print("[ERROR]", type(exc).__name__, exc)

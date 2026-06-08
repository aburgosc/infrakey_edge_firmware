# hal.py
# Abstraccion de hardware: mismo API para hardware real y mock.

try:
    import ujson as json
except Exception:
    import json

import os

# -------- util shim de tiempo --------
try:
    import utime as _tm
except Exception:
    import time as _t
    class _Shim:
        def sleep(self, s): _t.sleep(s)
        def sleep_ms(self, ms): _t.sleep(ms / 1000.0)
        def ticks_ms(self): return int(_t.time() * 1000)
        def ticks_diff(self, a, b): return a - b
    _tm = _Shim()

def _getenv(name, default=None):
    try:
        return os.getenv(name, default)
    except Exception:
        return default


def _safe_float(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


def _battery_percent(voltage, empty_v=3.3, full_v=4.2):
    try:
        v = float(voltage)
        empty_v = float(empty_v)
        full_v = float(full_v)
        if full_v <= empty_v:
            return None
        pct = int(((v - empty_v) * 100.0) / (full_v - empty_v))
        if pct < 0:
            return 0
        if pct > 100:
            return 100
        return pct
    except Exception:
        return None

# =============== Mock HAL ===============
class MockHAL:
    def __init__(self, debug=1):
        from .mock import SIM7080Modem
        self.debug = debug
        self.m = SIM7080Modem(debug=debug)
        self.current_sock_id = 0

    class _PinOutMock:
        def __init__(self, name, active_high=True, initial=False, debug=1):
            self.name = name; self.active_high = active_high
            self.state = bool(initial); self.debug = debug
        def on(self):  self.state = True;  print(f"[mock GPIO] {self.name}=ON") if self.debug else None
        def off(self): self.state = False; print(f"[mock GPIO] {self.name}=OFF") if self.debug else None
        def value(self, v=None):
            if v is None: return 1 if self.state else 0
            self.state = bool(v)

    class _PinInMock:
        def __init__(self, name, pull=None, debug=1):
            self.name = name; self.state = 0; self.debug = debug
        def read(self): return self.state
        def _set(self, v): self.state = 1 if v else 0

    class _PwmMock:
        def __init__(self, pin_no, freq_hz=50, debug=1):
            self.pin_no = pin_no
            self.freq_hz = int(freq_hz)
            self.last_duty = 0
            self.debug = debug
        def freq(self, hz):
            self.freq_hz = int(hz)
        def duty_u16(self, val):
            self.last_duty = int(val)
            if self.debug >= 2:
                print("[mock PWM] pin={} duty={}".format(self.pin_no, self.last_duty))
        def deinit(self):
            if self.debug >= 2:
                print("[mock PWM] pin={} deinit".format(self.pin_no))

    def set_sock_id(self, cid:int): self.current_sock_id = int(cid)

    def blink(self, n=2, t=0.12):
        if self.debug: print(f"[mock] blink x{n}")
        for _ in range(n): _tm.sleep(t)

    def pwr_pulse(self):
        if self.debug >= 2: print("[mock] pwr_pulse")

    def pin_out(self, pin_no, active_high=True, initial=False):
        return self._PinOutMock(f"OUT{pin_no}", active_high, initial, self.debug)

    def pin_in(self, pin_no, pull=None):
        return self._PinInMock(f"IN{pin_no}", pull, self.debug)

    def pwm_out(self, pin_no, freq_hz=50):
        return self._PwmMock(pin_no, freq_hz=freq_hz, debug=self.debug)

    # --- AT / RAW ---
    def send_at(self, cmd, expect="OK", timeout=1500, dump=False):
        if not str(cmd).startswith("AT"): cmd = "AT" + str(cmd)
        return self.m.at(cmd, expect=expect, timeout=timeout, dump=dump)
    
    def write_at(self, cmd):
        s = cmd if str(cmd).startswith("AT") else ("AT" + str(cmd))
        if self.debug >= 1:
            print(">>", s)
        try:
            self.m.write_at_only(s + "\r\n")
        except Exception:
            pass

    def write_raw(self, data: bytes):
        self.m.socket_send(data, wait_ok_ms=8000); return True

    def carecv_once_exact(self, ask_len, overall_ms=8000):
        return self.m.carecv_once_exact(ask_len, overall_ms)

    def uart_read_some(self, max_bytes=256): return b""
    def get_imei(self): return self.m.get_imei()
    def get_iccid(self): return self.m.get_iccid()
    def read_battery_voltage(self, adc_pin=None, divider_ratio=2.0, vref=3.3, samples=4):
        v = _safe_float(_getenv("SIM7080_MOCK_BATTERY_V", 3.7), None)
        return v
    def battery_percent(self, voltage, empty_v=3.3, full_v=4.2):
        return _battery_percent(voltage, empty_v=empty_v, full_v=full_v)
    def now_utc_iso(self):
        forced = _getenv("SIM7080_MOCK_UTC", None)
        if forced:
            return forced
        return None
    def ticks_ms(self): return _tm.ticks_ms()
    def ticks_diff(self, a, b): return _tm.ticks_diff(a, b)
    def sleep(self, s): _tm.sleep(s)
    def sleep_ms(self, ms): _tm.sleep_ms(ms)

# =============== Hardware HAL ===============
class HardwareHAL:
    """
    HAL para Raspberry Pi Pico (MicroPython + machine).
    Maneja UART, pines, y lectura AT+CARECV exacta.
    """
    def __init__(self, uart_port=0, baud=115200, led_pin=25, pwr_en_pin=14, debug=1):
        import machine
        self.debug = debug
        self._machine = machine
        self.uart = machine.UART(uart_port, baud)
        self.led = machine.Pin(led_pin, machine.Pin.OUT)
        self.pwr_en_pin = pwr_en_pin
        self.current_sock_id = 0

    class _PinOutHW:
        def __init__(self, machine, pin_no, active_high=True, initial=False):
            self.active_high = active_high
            self._pin = machine.Pin(pin_no, machine.Pin.OUT)
            self._pin.value(1 if (initial and active_high) else 0)
        def on(self):  self._pin.value(1 if self.active_high else 0)
        def off(self): self._pin.value(0 if self.active_high else 1)
        def value(self, v=None):
            if v is None: return self._pin.value()
            self._pin.value(int(v))

    class _PinInHW:
        def __init__(self, machine, pin_no, pull=None):
            pull_kw = None
            if pull == "up":   pull_kw = machine.Pin.PULL_UP
            if pull == "down": pull_kw = machine.Pin.PULL_DOWN
            self._pin = machine.Pin(pin_no, machine.Pin.IN, pull_kw) if pull_kw else machine.Pin(pin_no, machine.Pin.IN)
        def read(self): return self._pin.value()

    def set_sock_id(self, cid:int):
        try: self.current_sock_id = int(cid)
        except Exception: self.current_sock_id = 0

    def pin_out(self, pin_no, active_high=True, initial=False):
        return self._PinOutHW(self._machine, pin_no, active_high, initial)

    def pin_in(self, pin_no, pull=None):
        return self._PinInHW(self._machine, pin_no, pull)

    def pwm_out(self, pin_no, freq_hz=50):
        pwm = self._machine.PWM(self._machine.Pin(pin_no))
        try:
            pwm.freq(int(freq_hz))
        except Exception:
            pass
        return pwm

    # UI / energia
    def blink(self, n=2, t=0.12):
        for _ in range(n):
            self.led.value(1); _tm.sleep(t)
            self.led.value(0); _tm.sleep(t)

    def pwr_pulse(self):
        try:
            p = self._machine.Pin(self.pwr_en_pin, self._machine.Pin.OUT)
            p.value(1); _tm.sleep(1.8); p.value(0)
        except Exception: pass

    # UART raw
    def uart_read_some(self, max_bytes=256):
        if self.uart.any(): return self.uart.read(max_bytes) or b""
        return b""

    def write_raw(self, data: bytes):
        self.uart.write(data); return True
        
    def write_at(self, cmd):
        s = cmd if str(cmd).startswith("AT") else ("AT" + str(cmd))
        if self.debug >= 1:
            print(">>", s)
        self.uart.write((s + "\r\n").encode())

    # AT
    def send_at(self, cmd, expect="OK", timeout=1500, dump=False):
        cmd_to_send = cmd if str(cmd).startswith("AT") else ("AT" + str(cmd))
        if self.debug >= 1: print(">>", cmd_to_send)
        self.uart.write((cmd_to_send + "\r\n").encode())

        buf = b""; t0 = _tm.ticks_ms()
        while _tm.ticks_diff(_tm.ticks_ms(), t0) < timeout:
            chunk = self.uart_read_some(256)
            if chunk:
                buf += chunk
                if dump and self.debug >= 2:
                    try: print(chunk.decode("utf-8","ignore"), end="")
                    except Exception: pass
            else:
                _tm.sleep_ms(10)

        resp = buf.decode("utf-8","ignore") if buf else ""
        if self.debug >= 2 and resp:
            for ln in resp.splitlines(): print("<<", ln)

        if expect is None: return True, resp
        if resp and expect in resp: return True, resp
        if resp and "ERROR" in resp: return False, resp
        return False, resp

    def carecv_once_exact(self, ask_len, overall_ms=8000):
        """
        LECTURA EXACTA SIN usar send_at(): escribimos crudo 'AT+CARECV=cid,len'
        y luego parseamos '+CARECV: <len>,' seguido de exactamente <len> bytes.
        """
        cid = getattr(self, "current_sock_id", 0)

        # Escribir crudo (no usar send_at aqui)
        cmd = "AT+CARECV={},{}\r\n".format(cid, int(ask_len))
        if self.debug >= 1:
            print(">>", cmd.strip())
        self.uart.write(cmd.encode())

        buf = b""
        t0 = self.ticks_ms()
        token = b"+CARECV:"
        token_pos = -1
        comma_pos = -1
        recv_len = None

        while self.ticks_diff(self.ticks_ms(), t0) < overall_ms:
            chunk = self.uart_read_some(256)
            if chunk:
                buf += chunk

                # localizar el prefijo
                if token_pos < 0:
                    p = buf.find(token)
                    if p >= 0:
                        token_pos = p

                # cuando tengamos el token, buscar la coma que separa <len>,<datos>
                if token_pos >= 0 and comma_pos < 0:
                    c = buf.find(b",", token_pos)
                    if c >= 0:
                        comma_pos = c
                        try:
                            k = buf.find(b":", token_pos)
                            length_field = buf[k + 1:comma_pos].strip()
                            digits = bytearray()
                            for ch in length_field:
                                if 48 <= ch <= 57:
                                    digits.append(ch)
                            recv_len = int(digits.decode()) if digits else None
                        except Exception:
                            recv_len = None

                # cuando ya sabemos el len y tenemos la coma, vemos si alcanzan los bytes
                if recv_len is not None and comma_pos >= 0:
                    data_start = comma_pos + 1
                    have = len(buf) - data_start
                    if have >= recv_len:
                        payload = buf[data_start:data_start + recv_len]

                        # Drenar best-effort el 'OK' posterior SIN chupar mas datos
                        t1 = self.ticks_ms()
                        tmp = b""
                        while self.ticks_diff(self.ticks_ms(), t1) < 120:
                            more = self.uart_read_some(256)
                            if not more:
                                break
                            tmp += more
                            if b"\r\nOK\r\n" in tmp:
                                break
                        return payload
            else:
                self.sleep_ms(20)

        # timeout o parseo fallido
        return b""

    # Identidad
    def _digits_only(self, s): return "".join(ch for ch in s if ch.isdigit())
    def get_imei(self):
        ok, r = self.send_at("+GSN","OK",2000); return self._digits_only(r) if ok else ""
    def get_iccid(self):
        ok, r = self.send_at("+CCID","OK",2000); return self._digits_only(r) if ok else ""

    def read_battery_voltage(self, adc_pin=None, divider_ratio=2.0, vref=3.3, samples=4):
        if adc_pin is None:
            return None
        try:
            try:
                adc = self._machine.ADC(int(adc_pin))
            except Exception:
                adc = self._machine.ADC(self._machine.Pin(int(adc_pin)))
            total = 0
            count = max(1, int(samples))
            for _ in range(count):
                total += int(adc.read_u16())
                self.sleep_ms(2)
            raw = total / float(count)
            return (raw / 65535.0) * float(vref) * float(divider_ratio)
        except Exception:
            return None

    def battery_percent(self, voltage, empty_v=3.3, full_v=4.2):
        return _battery_percent(voltage, empty_v=empty_v, full_v=full_v)

    def now_utc_iso(self):
        try:
            rtc = self._machine.RTC()
            dt = rtc.datetime()
            year = int(dt[0])
            if year < 2024:
                return None
            month = int(dt[1])
            day = int(dt[2])
            hour = int(dt[4])
            minute = int(dt[5])
            second = int(dt[6])
            return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (
                year,
                month,
                day,
                hour,
                minute,
                second,
            )
        except Exception:
            return None

    # tiempo
    def ticks_ms(self): return _tm.ticks_ms()
    def ticks_diff(self, a, b): return _tm.ticks_diff(a, b)
    def sleep(self, s): _tm.sleep(s)
    def sleep_ms(self, ms): _tm.sleep_ms(ms)

# =============== Factory ===============
def make_hal(debug=1, uart_port=0, baud=115200, led_pin=25, pwr_en_pin=14):
    use_mock = _getenv("SIM7080_USE_MOCK", None)
    if use_mock == "1": return MockHAL(debug=debug)
    try:
        import machine  # noqa: F401
        return HardwareHAL(uart_port=uart_port, baud=baud, led_pin=led_pin, pwr_en_pin=pwr_en_pin, debug=debug)
    except Exception:
        return MockHAL(debug=debug)



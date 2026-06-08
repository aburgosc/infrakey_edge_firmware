# sim7080mini/actuator.py
# Control de actuador:
# - Modo "relay": 1 pin (pulso) o 2 pines (open/close).
# - Modo "servo": PWM 50Hz (open_us / close_us), con drive_ms para soltar pulso.
# Sensor digital:
# - Si se define "sensor_pin", usa 0/1 con debounce para estado y tamper forzado.
# - Si no, mantiene compatibilidad con "tamper_pin" (activo alto/bajo).

try:
    import utime as _time
except Exception:
    import time as _time


def _ticks_ms():
    try:
        return _time.ticks_ms()
    except Exception:
        return int(_time.time() * 1000)


def _ticks_diff(a, b):
    try:
        return _time.ticks_diff(a, b)
    except Exception:
        return a - b


class _EdgeDebouncer:
    def __init__(self, read_fn, debounce_ms=60):
        self._read = read_fn
        self._db = int(debounce_ms)
        self._last = self._read()
        self._t_last = _ticks_ms()

    def edge(self):
        """
        Devuelve (changed:bool, new_val:int|None) con debounce.
        """
        now = _ticks_ms()
        v = 1 if self._read() else 0
        if v != self._last and _ticks_diff(now, self._t_last) >= self._db:
            self._last = v
            self._t_last = now
            return True, v
        return False, None

    def value(self):
        return 1 if self._read() else 0


class _ServoDriver:
    """
    Driver PWM generico. Intenta usar hal.pwm_out; fallback a machine.PWM si existe.
    Requiere:
      - .freq(hz)
      - .duty_u16(val)
      - .deinit()
    """
    def __init__(self, hal, pin, freq_hz=50):
        self._hal = hal
        self._pwm = None
        self._using_hal = False

        # 1) HAL con PWM, si existe
        try:
            if hasattr(hal, "pwm_out"):
                self._pwm = hal.pwm_out(pin, freq_hz=freq_hz)
                self._using_hal = True
        except Exception:
            self._pwm = None

        # 2) Fallback: machine.PWM
        if self._pwm is None:
            try:
                import machine
                self._pwm = machine.PWM(machine.Pin(pin))
                self._pwm.freq(int(freq_hz))
            except Exception:
                self._pwm = None

        if self._pwm is None:
            raise RuntimeError("PWM no disponible: no se pudo inicializar servo en pin {}".format(pin))

        self._freq = int(freq_hz)

    def _period_us(self):
        return int(1_000_000 // self._freq)

    def duty_from_us(self, us):
        period = self._period_us()
        if us < 0:
            us = 0
        if us > period:
            us = period
        duty = int(us * 65535 // period)
        if duty < 0:
            duty = 0
        if duty > 65535:
            duty = 65535
        return duty

    def pulse_us(self, us):
        duty = self.duty_from_us(us)
        # compatibilidad para ambos PWM
        if hasattr(self._pwm, "duty_u16"):
            self._pwm.duty_u16(duty)
        else:
            # si el HAL expone otra API, intenta duty16/duty
            if hasattr(self._pwm, "duty16"):
                self._pwm.duty16(duty)
            elif hasattr(self._pwm, "duty"):
                # duty (0..1023) aproximado
                self._pwm.duty(int(duty * 1023 // 65535))

    def idle(self):
        # poner duty=0 si soporta
        try:
            if hasattr(self._pwm, "duty_u16"):
                self._pwm.duty_u16(0)
            elif hasattr(self._pwm, "duty16"):
                self._pwm.duty16(0)
            elif hasattr(self._pwm, "duty"):
                self._pwm.duty(0)
        except Exception:
            pass

    def deinit(self):
        try:
            if hasattr(self._pwm, "deinit"):
                self._pwm.deinit()
        except Exception:
            pass


class Actuator:
    """
    API estable usada por handlers/main:

      open() / close()
      tamper_triggered() -> bool  (apertura forzada)

    Modos de operacion:

    - RELAY (por defecto, compat):
        "actuator_pin" (+ "actuator_active_high", "actuator_pulse_ms")
        o "open_pin"/"close_pin" (doble rele).

    - SERVO:
        "servo_pwm_pin": 20,
        "servo_freq": 50,
        "servo_open_us": 2300,
        "servo_close_us": 700,
        "servo_drive_ms": 500

    Sensor de estado:
      Preferente: "sensor_pin", "sensor_open_is", "sensor_pull", "sensor_debounce_ms"
      Compat: "tamper_pin", "tamper_pull", "tamper_active_high"
    """
    def __init__(self, hal, gpio_cfg: dict):
        self.hal = hal
        self.cfg = gpio_cfg or {}
        self._mode = self.cfg.get("mode", None)  # "relay" | "servo" | None(auto)
        self._last_cmd = "close"

        # --- Detectar modo ---
        use_servo = False
        if self._mode == "servo":
            use_servo = True
        elif self._mode == "relay":
            use_servo = False
        else:
            # auto: si hay servo_pwm_pin -> servo, si no -> relay
            use_servo = ("servo_pwm_pin" in self.cfg)

        # --- Salidas ---
        if use_servo:
            sv_pin = int(self.cfg.get("servo_pwm_pin", 20))
            sv_freq = int(self.cfg.get("servo_freq", 50))
            self._sv_open_us = int(self.cfg.get("servo_open_us", 2300))
            self._sv_close_us = int(self.cfg.get("servo_close_us", 700))
            self._sv_drive_ms = int(self.cfg.get("servo_drive_ms", 500))
            self._servo = _ServoDriver(hal, sv_pin, freq_hz=sv_freq)
            self._is_relay = False
        else:
            # Relay simple o doble (compat)
            self._is_relay = True
            self._single = "actuator_pin" in self.cfg and "open_pin" not in self.cfg
            self._pulse_ms = int(self.cfg.get("actuator_pulse_ms", 300))

            if self._single:
                self._pin = hal.pin_out(
                    self.cfg["actuator_pin"],
                    active_high=self.cfg.get("actuator_active_high", True),
                    initial=False,
                )
            else:
                # doble rele
                self._open_pin = hal.pin_out(self.cfg["open_pin"], active_high=True, initial=False)
                self._close_pin = hal.pin_out(self.cfg["close_pin"], active_high=True, initial=False)

        # --- Sensor / Tamper ---
        self._has_sensor = "sensor_pin" in self.cfg
        if self._has_sensor:
            # Nuevo: sensor de puerta con 0/1 y debounce
            pin = int(self.cfg.get("sensor_pin", 15))
            pull = self.cfg.get("sensor_pull", None)   # "up" | "down" | None
            self._sensor_open_is = 1 if int(self.cfg.get("sensor_open_is", 1)) else 0
            db = int(self.cfg.get("sensor_debounce_ms", 60))
            self._sensor = hal.pin_in(pin, pull=pull)
            self._sensor_db = _EdgeDebouncer(self._sensor.read, debounce_ms=db)
        else:
            # Compat: tamper pin (booleano activo alto/bajo)
            self._tamper = hal.pin_in(self.cfg["tamper_pin"], pull=self.cfg.get("tamper_pull", "up"))
            self._tamper_active_high = bool(self.cfg.get("tamper_active_high", True))

    # ------------- Relay helpers -------------
    def _pulse(self, pinobj, ms):
        pinobj.on()
        self.hal.sleep_ms(ms)
        pinobj.off()

    # ------------- API publica -------------
    def open(self):
        if self._is_relay:
            if getattr(self, "_single", False):
                self._pulse(self._pin, self._pulse_ms)
            else:
                self._pulse(self._open_pin, self._pulse_ms)
        else:
            # Servo
            self._servo.pulse_us(self._sv_open_us)
            if self._sv_drive_ms > 0:
                self.hal.sleep_ms(self._sv_drive_ms)
                self._servo.idle()
        self._last_cmd = "open"

    def close(self):
        if self._is_relay:
            if getattr(self, "_single", False):
                self._pulse(self._pin, self._pulse_ms)
            else:
                self._pulse(self._close_pin, self._pulse_ms)
        else:
            # Servo
            self._servo.pulse_us(self._sv_close_us)
            if self._sv_drive_ms > 0:
                self.hal.sleep_ms(self._sv_drive_ms)
                self._servo.idle()
        self._last_cmd = "close"

    def pulse(self, ms=500):
        ms = int(ms)
        if self._is_relay:
            pulse_ms = ms if ms > 0 else self._pulse_ms
            if getattr(self, "_single", False):
                self._pulse(self._pin, pulse_ms)
            else:
                self._pulse(self._open_pin, pulse_ms)
        else:
            self.open()
            self.hal.sleep_ms(ms if ms > 0 else self._sv_drive_ms)
            self.close()

    # Estado abierto/cerrado solo si hay sensor explicito
    def is_open(self):
        if not self._has_sensor:
            return None
        val = self._sensor_db.value()
        return 1 if val == self._sensor_open_is else 0

    def tamper_triggered(self) -> bool:
        """
        Modo sensor:
          True si el sensor cambio a ABIERTO y el ultimo comando NO fue 'open' (apertura forzada).
        Modo tamper compat:
          Lee el pin tamper con activo alto/bajo.
        """
        if self._has_sensor:
            changed, new_val = self._sensor_db.edge()
            if not changed or new_val is None:
                return False
            opened = (new_val == self._sensor_open_is)
            if opened and self._last_cmd != "open":
                return True
            return False
        else:
            val = 1 if self._tamper.read() else 0
            return bool(val) if self._tamper_active_high else not bool(val)

    def deinit(self):
        if not self._is_relay:
            try:
                self._servo.deinit()
            except Exception:
                pass



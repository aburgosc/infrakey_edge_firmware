try:
    import machine
    import utime as time
except Exception as exc:
    raise RuntimeError("Este script debe ejecutarse en la Pico con MicroPython") from exc


# Mapeos validos de UART en RP2040.
# Se prueban solo pares TX/RX validos para evitar combinaciones inutiles.
UART_CANDIDATES = [
    {"uart": 0, "tx": 0, "rx": 1},
    {"uart": 0, "tx": 12, "rx": 13},
    {"uart": 0, "tx": 16, "rx": 17},
    {"uart": 0, "tx": 28, "rx": 29},
    {"uart": 1, "tx": 4, "rx": 5},
    {"uart": 1, "tx": 8, "rx": 9},
    {"uart": 1, "tx": 20, "rx": 21},
    {"uart": 1, "tx": 24, "rx": 25},
]

# El SIM7080 suele estar en 115200. Se agrega 9600 por descarte basico.
BAUD_CANDIDATES = [115200, 9600]


def _read_uart(uart, timeout_ms=1200):
    buf = b""
    start = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
        if uart.any():
            chunk = uart.read()
            if chunk:
                buf += chunk
        time.sleep_ms(20)
    return buf


def _probe_once(uart_id, tx_pin, rx_pin, baud):
    try:
        tx = machine.Pin(tx_pin)
        rx = machine.Pin(rx_pin)
        uart = machine.UART(uart_id, baudrate=baud, tx=tx, rx=rx)
    except Exception as exc:
        return False, "init_error: {}".format(exc)

    try:
        # Limpia basura previa
        _read_uart(uart, timeout_ms=150)

        for _ in range(3):
            uart.write(b"AT\r\n")
            resp = _read_uart(uart, timeout_ms=700)
            if resp:
                text = resp.decode("utf-8", "ignore")
                if ("OK" in text) or ("ERROR" in text):
                    return True, text

        # Una lectura final por si hubo eco o basura util
        resp = _read_uart(uart, timeout_ms=400)
        if resp:
            return True, resp.decode("utf-8", "ignore")

        return False, ""
    finally:
        try:
            uart.deinit()
        except Exception:
            pass


def run_probe():
    print("=== UART probe RP2040 -> SIM7080 ===")
    print("Se probaran pares TX/RX validos del RP2040.")
    print("Este script NO prueba PWRKEY ni otros GPIO por seguridad.")
    print("")

    hits = []
    for baud in BAUD_CANDIDATES:
        print("--- Baud {} ---".format(baud))
        for item in UART_CANDIDATES:
            uart_id = item["uart"]
            tx_pin = item["tx"]
            rx_pin = item["rx"]
            print("Probing UART{} TX=GP{} RX=GP{} ...".format(uart_id, tx_pin, rx_pin), end="")
            ok, resp = _probe_once(uart_id, tx_pin, rx_pin, baud)
            if ok:
                print(" HIT")
                print("Respuesta:")
                print(resp if resp else "<sin texto decodificable>")
                hits.append(
                    {
                        "uart": uart_id,
                        "tx": tx_pin,
                        "rx": rx_pin,
                        "baud": baud,
                        "resp": resp,
                    }
                )
            else:
                print(" no response")
        print("")

    print("=== Resultado ===")
    if not hits:
        print("No hubo respuesta en ningun UART/pin probado.")
        print("Si el modulo requiere secuencia de encendido, este script no la ejecuta.")
        print("Tambien puede ser falta de alimentacion, PWRKEY, GND comun o nivel logico incorrecto.")
        return

    for idx, hit in enumerate(hits, 1):
        print(
            "{}. UART{} TX=GP{} RX=GP{} baud={} resp={!r}".format(
                idx, hit["uart"], hit["tx"], hit["rx"], hit["baud"], hit["resp"]
            )
        )


run_probe()

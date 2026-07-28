try:
    import machine
except Exception:
    machine = None

from sim7080mini.config import load_config
from sim7080mini.hal import make_hal


def _print_banner(cfg, hw):
    print("[test] Inicio prueba UART SIM7080")
    print("[test] uart_port=", hw.get("uart_port"))
    print("[test] baud=", hw.get("baud"))
    print("[test] uart_tx_pin=", hw.get("uart_tx_pin"))
    print("[test] uart_rx_pin=", hw.get("uart_rx_pin"))
    print("[test] pwr_en_pin=", hw.get("pwr_en_pin"))
    print("[test] debug=", cfg.get("debug", 1))


def _build_hal(cfg):
    hw = cfg.get("hardware", {})
    return make_hal(
        debug=cfg.get("debug", 1),
        uart_port=hw.get("uart_port", 0),
        baud=hw.get("baud", 115200),
        led_pin=hw.get("led_pin", 25),
        pwr_en_pin=hw.get("pwr_en_pin", 14),
        uart_tx_pin=hw.get("uart_tx_pin", None),
        uart_rx_pin=hw.get("uart_rx_pin", None),
    )


def _try_at(hal, cmd, expect="OK", timeout=2000):
    ok, resp = hal.send_at(cmd, expect, timeout)
    print("[test] cmd=", cmd, "ok=", ok)
    if resp:
        print(resp)
    else:
        print("[test] sin respuesta")
    return ok, resp


def main():
    cfg = load_config("device_config.json")
    hw = cfg.setdefault("hardware", {})

    # Forzamos UART0 para esta prueba puntual.
    hw["uart_port"] = 0
    if hw.get("uart_tx_pin", None) is None:
        hw["uart_tx_pin"] = 0
    if hw.get("uart_rx_pin", None) is None:
        hw["uart_rx_pin"] = 1

    _print_banner(cfg, hw)
    hal = _build_hal(cfg)

    print("[test] pulso de encendido en pwr_en_pin")
    try:
        hal.pwr_pulse()
        hal.sleep(3)
    except Exception as exc:
        print("[test] pwr_pulse fallo:", exc)

    commands = [
        ("AT", "OK", 2000),
        ("ATE0", "OK", 2000),
        ("AT+CMEE=2", "OK", 2000),
        ("AT+CPIN?", "OK", 2500),
        ("AT+CSQ", "OK", 2500),
    ]

    ok_count = 0
    for cmd, expect, timeout in commands:
        ok, _ = _try_at(hal, cmd, expect, timeout)
        if ok:
            ok_count += 1
        hal.sleep_ms(250)

    print("[test] resumen ok=", ok_count, "de", len(commands))
    if ok_count == 0:
        print("[test] sin respuesta AT: revisar energia, UART0 GP0/GP1, GND comun y pin de encendido")
    elif ok_count < len(commands):
        print("[test] comunicacion parcial: revisar estado SIM, baud y estabilidad de alimentacion")
    else:
        print("[test] modem responde correctamente por UART0")

    if machine is not None:
        print("[test] fin")


main()

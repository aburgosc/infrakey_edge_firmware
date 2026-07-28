try:
    import ujson as json
except Exception:
    import json

from sim7080mini.config import load_config
from sim7080mini.hal import make_hal
from sim7080mini.modem import SIM7080


TEST_HOST = "jsonplaceholder.typicode.com"
TEST_PORT = 443
TEST_PATH = "/todos/1"
TEST_USER_AGENT = "infrakey-pico-internet-probe/1.0"


def _print_result(status, obj, body):
    print("")
    print("=== Resultado HTTP ===")
    print("status =", status)

    if obj is not None:
        try:
            print("json =", json.dumps(obj))
        except Exception:
            print("json =", obj)
    elif body:
        print("body =", body[:500])
    else:
        print("body = <vacio>")

    if status == 200:
        print("")
        print("[OK] La Pico tiene salida a Internet y HTTPS funciona.")
        return

    if status == 0:
        print("")
        print("[FAIL] No se obtuvo una respuesta HTTP.")
        print("Revise el ultimo mensaje [pdp], [dns], [tls] o [sock].")
        return

    print("")
    print("[FAIL] Internet respondio, pero el servidor devolvio HTTP", status)


def main():
    cfg = load_config("device_config.json")
    hw = cfg.get("hardware", {})
    debug = cfg.get("debug", 2)

    print("=== Internet probe SIM7080 ===")
    print("URL = https://{}{}".format(TEST_HOST, TEST_PATH))
    print(
        "UART{} TX=GP{} RX=GP{} baud={}".format(
            hw.get("uart_port", 0),
            hw.get("uart_tx_pin", 0),
            hw.get("uart_rx_pin", 1),
            hw.get("baud", 115200),
        )
    )
    print("APN fallback =", cfg.get("apn_fallback", "m2m.entel.cl"))
    print("")

    hal = make_hal(
        debug=debug,
        uart_port=hw.get("uart_port", 0),
        baud=hw.get("baud", 115200),
        led_pin=hw.get("led_pin", 25),
        pwr_en_pin=hw.get("pwr_en_pin", 14),
        uart_tx_pin=hw.get("uart_tx_pin", 0),
        uart_rx_pin=hw.get("uart_rx_pin", 1),
    )
    modem = SIM7080(
        hal,
        nb_band=cfg.get("nb_band", 28),
        tls_ctx=0,
        sock_id=0,
        debug=debug,
    )

    if not modem.start():
        print("[FAIL] El modem no responde a comandos AT.")
        return

    modem.set_radio_nbiot()

    if not modem.attach_and_pdp(
        fallback_apn=cfg.get("apn_fallback", "m2m.entel.cl"),
        wait_ms=120000,
    ):
        print("[FAIL] No fue posible registrar o activar el contexto PDP.")
        return

    print("")
    print("[http] Ejecutando GET", TEST_PATH)
    status, obj, body = modem.http_get_json_return(
        host=TEST_HOST,
        connect_host=None,
        port=TEST_PORT,
        user_agent=TEST_USER_AGENT,
        path=TEST_PATH,
        extra_headers={},
    )
    _print_result(status, obj, body)


try:
    main()
except KeyboardInterrupt:
    print("")
    print("[STOP] Prueba interrumpida por el usuario.")
except Exception as exc:
    print("")
    print("[ERROR]", type(exc).__name__, exc)


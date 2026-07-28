try:
    import utime as _time
except Exception:
    import time as _time

from sim7080mini.config import load_config
from sim7080mini.hal import make_hal
from sim7080mini.modem import SIM7080


def _parse_cgnsinf(resp):
    for line in (resp or "").splitlines():
        if "+CGNSINF:" not in line:
            continue
        body = line.split(":", 1)[1].strip()
        parts = [p.strip() for p in body.split(",")]
        if len(parts) < 5:
            return None
        try:
            run_status = int(parts[0] or "0")
        except Exception:
            run_status = 0
        try:
            fix_status = int(parts[1] or "0")
        except Exception:
            fix_status = 0
        utc = parts[2] if len(parts) > 2 else ""
        lat = parts[3] if len(parts) > 3 else ""
        lon = parts[4] if len(parts) > 4 else ""
        alt = parts[5] if len(parts) > 5 else ""
        speed = parts[6] if len(parts) > 6 else ""
        course = parts[7] if len(parts) > 7 else ""
        sats = parts[14] if len(parts) > 14 else ""
        return {
            "run_status": run_status,
            "fix_status": fix_status,
            "utc": utc,
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "speed": speed,
            "course": course,
            "sats": sats,
            "raw": body,
        }
    return None


def _print_fix(info):
    print("[gps] run_status =", info.get("run_status"))
    print("[gps] fix_status =", info.get("fix_status"))
    print("[gps] utc =", info.get("utc") or "<none>")
    print("[gps] lat =", info.get("lat") or "<none>")
    print("[gps] lon =", info.get("lon") or "<none>")
    print("[gps] alt =", info.get("alt") or "<none>")
    print("[gps] speed =", info.get("speed") or "<none>")
    print("[gps] course =", info.get("course") or "<none>")
    print("[gps] sats =", info.get("sats") or "<none>")


def _sync_modem(modem):
    print("[gps] sincronizando modem")
    if not modem.start():
        print("[gps] no fue posible sincronizar el modem")
        return False
    return True


def _send_and_print(hal, cmd, expect="OK", timeout=2000):
    ok, resp = hal.send_at(cmd, expect, timeout)
    print("[gps] cmd =", cmd, "ok =", ok)
    if resp:
        print(resp)
    return ok, resp


def main():
    cfg = load_config("device_config.json")
    debug = cfg.get("debug", 1)
    hw = cfg.get("hardware", {})

    print("=== GPS probe SIM7080 ===")
    print("[gps] Nota: el firmware actual usa coordenadas estaticas de config.")
    print("[gps] Este probe valida GNSS real del modem via AT.")

    hal = make_hal(
        debug=debug,
        uart_port=hw.get("uart_port", 0),
        baud=hw.get("baud", 115200),
        led_pin=hw.get("led_pin", 25),
        pwr_en_pin=hw.get("pwr_en_pin", 14),
        uart_tx_pin=hw.get("uart_tx_pin"),
        uart_rx_pin=hw.get("uart_rx_pin"),
    )
    modem = SIM7080(hal, nb_band=cfg.get("nb_band", 28), tls_ctx=0, sock_id=0, debug=debug)

    if not _sync_modem(modem):
        return

    _send_and_print(hal, "AT+CGNSPWR?", "OK", 2000)
    ok_pwr, _ = _send_and_print(hal, "AT+CGNSPWR=1", "OK", 5000)
    if not ok_pwr:
        print("[gps] no fue posible encender GNSS")
        return

    print("[gps] GNSS encendido. Esperando fix...")
    print("[gps] Recomendacion: antena GNSS conectada y vision de cielo abierto.")

    max_polls = 18
    poll_delay_sec = 5
    last_info = None

    for idx in range(1, max_polls + 1):
        ok_inf, resp_inf = hal.send_at("AT+CGNSINF", "OK", 3000)
        print("[gps] poll", idx, "ok =", ok_inf)
        if resp_inf:
            print(resp_inf)
        info = _parse_cgnsinf(resp_inf)
        if info:
            last_info = info
            _print_fix(info)
            lat = info.get("lat") or ""
            lon = info.get("lon") or ""
            if info.get("fix_status") == 1 and lat not in ("", "0", "0.0") and lon not in ("", "0", "0.0"):
                print("[gps] FIX valido obtenido")
                return
        if idx < max_polls:
            _time.sleep(poll_delay_sec)

    print("[gps] no se obtuvo fix dentro del tiempo esperado")
    if last_info:
        print("[gps] ultimo estado observado:")
        _print_fix(last_info)
    print("[gps] revisar antena GNSS, ubicacion exterior y tiempo de primer fix")


main()

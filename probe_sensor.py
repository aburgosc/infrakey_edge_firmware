"""
Probe seguro de polaridad para el sensor de puerta en GP15.

No mueve el servo, no arranca el modem y no usa archivos del proyecto.
"""

try:
    import machine
    import utime as time
except Exception as exc:
    raise RuntimeError("Ejecutar este archivo en la Pico con MicroPython") from exc


SENSOR_PIN = 15
PULL_MODES = ("down", "up")
SAMPLES = 20
SAMPLE_INTERVAL_MS = 50


def _make_pin(pull_mode):
    if pull_mode == "up":
        pull = machine.Pin.PULL_UP
    elif pull_mode == "down":
        pull = machine.Pin.PULL_DOWN
    else:
        pull = None

    if pull is None:
        return machine.Pin(SENSOR_PIN, machine.Pin.IN)
    return machine.Pin(SENSOR_PIN, machine.Pin.IN, pull)


def _sample_stable(pin, label, pull_mode):
    zeros = 0
    ones = 0
    values = []

    for _ in range(SAMPLES):
        value = 1 if pin.value() else 0
        values.append(value)
        if value:
            ones += 1
        else:
            zeros += 1
        time.sleep_ms(SAMPLE_INTERVAL_MS)

    stable = 1 if ones > zeros else 0
    changes = 0
    for index in range(1, len(values)):
        if values[index] != values[index - 1]:
            changes += 1

    print(
        "[sensor] {} pull={}: value={} zeros={} ones={} changes={}".format(
            label,
            pull_mode,
            stable,
            zeros,
            ones,
            changes,
        )
    )
    return stable, changes


def _sample_state(label):
    results = {}
    for pull_mode in PULL_MODES:
        pin = _make_pin(pull_mode)
        time.sleep_ms(100)
        results[pull_mode] = _sample_stable(pin, label, pull_mode)
    return results


def main():
    print("=== Probe sensor de puerta GP15 ===")
    print("Este script NO mueve el actuador ni usa el modem.")
    print("Pin = GP{}; se probaran pull-down y pull-up.".format(SENSOR_PIN))
    print("")

    input("1. Deje la puerta/contacto CERRADO y presione Enter: ")
    closed = _sample_state("CERRADO")

    input("2. Abra o interrumpa el contacto y presione Enter: ")
    opened = _sample_state("ABIERTO")

    input("3. Cierre nuevamente el contacto y presione Enter: ")
    closed_again = _sample_state("CERRADO_FINAL")

    print("")
    print("=== Resultado ===")

    candidates = []
    for pull_mode in PULL_MODES:
        closed_value, closed_changes = closed[pull_mode]
        open_value, open_changes = opened[pull_mode]
        final_value, final_changes = closed_again[pull_mode]
        if closed_value != open_value and final_value == closed_value:
            noise = closed_changes + open_changes + final_changes
            candidates.append((noise, pull_mode, open_value))

    if not candidates:
        print("[FAIL] GP15 no cambia entre cerrado y abierto.")
        print("Revise cableado, GND comun, contacto y resistencia pull.")
        return

    candidates.sort(key=lambda item: (item[0], PULL_MODES.index(item[1])))
    noise, pull_mode, open_value = candidates[0]
    noisy = noise > 2
    print("[OK] Cambio de estado detectado y reversible.")
    print("")
    print("Configuracion recomendada:")
    print('"sensor_pin": {},'.format(SENSOR_PIN))
    print('"sensor_open_is": {},'.format(open_value))
    print('"sensor_pull": "{}",'.format(pull_mode))
    print('"sensor_debounce_ms": {}'.format(100 if noisy else 60))

    if noisy:
        print("[WARN] Se observo ruido; se recomienda debounce de 100 ms.")
    else:
        print("[OK] La senal se observo estable.")


try:
    main()
except KeyboardInterrupt:
    print("")
    print("[STOP] Prueba interrumpida.")
except Exception as exc:
    print("")
    print("[ERROR]", type(exc).__name__, exc)

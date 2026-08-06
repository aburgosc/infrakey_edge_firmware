# Infrakey Edge Firmware

Firmware MicroPython para Raspberry Pi Pico RP2040 con modem SIM7080. El dispositivo opera una compuerta fisica conectada a la plataforma Infrakey mediante API REST y WebSocket ActionCable.

La configuracion de referencia para terreno esta en `docs/device_config.terreno.example.json`.

## Que Hace El Dispositivo

El firmware permite:

- Registrar el dispositivo en Infrakey mediante `claim`.
- Enviar un `heartbeat` inicial al arrancar y luego heartbeats periodicos.
- Mantener conexion WebSocket para recibir comandos remotos.
- Ejecutar apertura y cierre remoto del actuador.
- Detectar apertura y cierre fisico mediante sensor magnetico.
- Reportar eventos de operacion y seguridad.
- Leer ubicacion real desde GNSS del modem.
- Responder comandos con ACK.
- Recuperarse ante fallas transitorias de red, modem o WebSocket.

## Plataforma

El firmware esta desarrollado para MicroPython sobre Raspberry Pi Pico RP2040 con modem SIM7080.

Requiere que el entorno MicroPython tenga soporte para:

- UART
- GPIO
- PWM
- temporizadores
- JSON
- control basico de memoria

## Flujo De Operacion

Al energizarse, el dispositivo:

1. Carga la configuracion.
2. Inicializa el modem.
3. Configura NB-IoT y APN.
4. Intenta obtener ubicacion GNSS real.
5. Se registra o reutiliza credenciales existentes.
6. Envia siempre un heartbeat inicial.
7. Abre el WebSocket si esta habilitado.
8. Entra al loop principal de comandos, sensor, eventos, heartbeat y reconexion.

El heartbeat inicial es obligatorio porque informa el estado de arranque del dispositivo a la plataforma.

## Integracion Con Infrakey

Gateway REST:

```text
https://api.infrakey.fasttrack.cloud
```

WebSocket:

```text
wss://infrakey.fasttrack.cloud/cable
```

Endpoints usados:

- `POST /api/v1/devices/claim`
- `POST /api/v1/devices/{device_id}/heartbeat`
- `POST /api/v1/devices/{device_id}/events`
- `POST /api/v1/devices/{device_id}/events/snapshot`
- `POST /api/v1/devices/{device_id}/commands/{id}/ack`
- `GET /api/v1/health`

Excepto `claim`, las llamadas usan token de autenticacion.

## Comandos Soportados

- `open_actuator`: autoriza y ejecuta apertura del actuador.
- `close_actuator`: ejecuta cierre del actuador y espera confirmacion fisica del sensor.
- `update_config`: actualiza parametros permitidos de operacion.
- `ping`: responde ACK inmediato.
- `snapshot`: envia estado general del dispositivo.

## Eventos Reportados

- `authorization_request`: solicitud de apertura recibida.
- `authorization_granted`: actuador autorizado y comandado a apertura.
- `device_opened`: apertura fisica confirmada por sensor.
- `device_closed`: cierre fisico confirmado por sensor.
- `tamper_alert`: alerta por condicion anomala.
- `unauthorized_access`: apertura no autorizada.
- `battery_low`: bateria bajo umbral.
- `device_offline`: fallas repetidas de comunicacion.
- `snapshot`: estado general solicitado por comando.

## Logica De Seguridad De La Compuerta

El sensor magnetico es la fuente confiable del estado fisico. El servo solo indica que se envio una orden, pero no confirma posicion real.

Estados internos principales:

- `door_state`: `unknown`, `open`, `closed`.
- `actuator_state`: `unknown`, `unlock_commanded`, `lock_commanded`.
- `security_state`: `unknown`, `locked`, `unlocked_authorized`, `locking_pending`.

Reglas:

- Despues de `open_actuator`, el estado pasa a `unlocked_authorized`.
- Mientras este `unlocked_authorized`, una apertura fisica no genera alarma.
- `device_opened` se emite solo cuando el sensor confirma apertura real.
- Despues de `close_actuator`, el estado pasa a `locking_pending`.
- `device_closed` se emite solo cuando el sensor confirma cierre real.
- Si el sensor abre estando `locked`, `unknown` o `locking_pending`, se reporta `tamper_alert` y `unauthorized_access`.
- Si el equipo arranca con sensor abierto, se reporta una alerta diferenciada como arranque anomalo.

## Configuracion

La configuracion de referencia esta en:

```text
docs/device_config.terreno.example.json
```

Ese archivo define conectividad, modem, WebSocket, GPS, sensor, actuador, heartbeat, debug y parametros de recuperacion.

### Conectividad

- `host`: nombre DNS usado para las llamadas REST contra Infrakey. Tambien se usa como `Host` HTTP y SNI TLS.
- `connect_host`: destino alternativo para abrir el socket REST. Puede ser `null`; si se configura, permite conectar por IP u otro host manteniendo `host` para SNI/HTTP.
- `port`: puerto TCP/TLS del gateway REST. En produccion debe ser `443`.
- `ws_host`: nombre DNS usado por ActionCable. Para produccion se usa `infrakey.fasttrack.cloud`.
- `ws_connect_host`: destino alternativo para abrir el socket WebSocket. Puede ser `null`; si se configura, mantiene `ws_host` para SNI/HTTP.
- `nb_band`: banda NB-IoT configurada en el SIM7080. Para la red usada en terreno se opera con banda `28`.
- `apn_fallback`: APN aplicado cuando el modem no entrega un APN valido o se requiere forzar configuracion de operador.
- `user_agent`: identificador HTTP enviado en las peticiones REST y WebSocket.
- `http_retry_count`: numero maximo de reintentos por operacion HTTP cuando hay error transitorio.
- `http_retry_backoff_ms`: espera entre reintentos HTTP, expresada en milisegundos.

### Identidad Y Telemetria

- `fw`: version de firmware reportada.
- `model`: modelo visible en plataforma.
- `latitude` y `longitude`: respaldo estatico. En produccion deben ser `null` si se exige GPS real.
- `heartbeat_interval_sec`: intervalo local de heartbeat.
- `heartbeat_interval_min_sec`: minimo aceptado.
- `heartbeat_interval_max_sec`: maximo aceptado.
- `next_pull_min_sec`: minimo aceptado desde backend.
- `next_pull_max_sec`: maximo aceptado desde backend.

El backend puede responder `next_pull_sec`, pero el firmware lo limita para respetar el intervalo local configurado.

Si `latitude` y `longitude` estan en `null`, la ubicacion enviada en `claim`, `heartbeat` y `snapshot` proviene exclusivamente del GNSS real o de cache GNSS real autorizado. Si no existe lectura real, el firmware reporta telemetria faltante y no inventa coordenadas.

### Features

- `features.allow_snapshot`: habilita la respuesta al comando `snapshot`. Si esta en `false`, el firmware no debe enviar snapshots aunque el comando llegue por WebSocket.

### GPS

- `gps.mode`: modo de ubicacion. Para produccion se usa `modem_gnss`.
- `gps.allow_static`: permite o bloquea coordenadas estaticas.
- `gps.include_source`: incluye origen de coordenadas.
- `gps.power_on_startup`: intenta leer GNSS al arrancar.
- `gps.power_down_after_read`: apaga GNSS luego de leer para ahorrar energia.
- `gps.poll_attempts`: intentos de lectura.
- `gps.poll_interval_ms`: espera entre intentos.
- `gps.cache_ms`: vigencia del cache GPS fresco.
- `gps.retry_ms`: espera minima antes de reintentar GNSS si no hubo fix.
- `gps.allow_stale_cache`: permite usar ultimo GPS real aunque el cache haya vencido si no hay lectura nueva.

En produccion, si no hay GPS real ni cache real, el firmware informa que falta GPS en vez de inventar coordenadas.

### Actuador Y Sensor

- `gpio.mode`: tipo de actuador conectado. `servo` usa PWM; `relay` usa una salida digital por pulso.
- `gpio.actuator_pin`: pin GPIO usado por el modo `relay`.
- `gpio.actuator_active_high`: polaridad del rele en modo `relay`. Si es `true`, nivel alto activa el rele.
- `gpio.actuator_pulse_ms`: duracion del pulso de activacion en modo `relay`.
- `gpio.servo_pwm_pin`: pin PWM usado para controlar el servo.
- `gpio.servo_freq`: frecuencia PWM. Para servo RC normalmente se usa `50 Hz`.
- `gpio.servo_open_us`: ancho de pulso PWM en microsegundos para la posicion mecanica de apertura.
- `gpio.servo_close_us`: ancho de pulso PWM en microsegundos para la posicion mecanica de cierre.
- `gpio.servo_drive_ms`: tiempo durante el cual se mantiene energizada la senal PWM antes de desactivar el pulso.
- `gpio.sensor_pin`: pin digital conectado al sensor magnetico de compuerta.
- `gpio.sensor_open_is`: valor logico leido cuando la compuerta esta abierta. `1` significa abierto activo alto; `0` significa abierto activo bajo.
- `gpio.sensor_pull`: resistencia interna aplicada al pin del sensor. Valores validos esperados: `up`, `down` o `null` segun cableado.
- `gpio.sensor_debounce_ms`: tiempo minimo de estabilidad para aceptar una transicion fisica y filtrar rebotes.
- `gpio.sensor_alert_if_open_on_boot`: habilita alerta diferenciada si el dispositivo arranca con el sensor indicando abierto.
- `gpio.sensor_boot_grace_ms`: ventana inicial antes de evaluar el estado abierto al arranque.
- `gpio.sensor_authorized_open_ms`: parametro legacy. Se conserva por compatibilidad, pero la politica actual de seguridad no depende de una ventana temporal.
- `gpio.tamper_pin`: pin legacy de tamper. Si no hay sensor magnetico dedicado, puede usarse como entrada de manipulacion.
- `gpio.tamper_pull`: resistencia interna del pin legacy de tamper.
- `gpio.tamper_active_high`: polaridad del pin legacy de tamper.

Los valores `servo_open_us` y `servo_close_us` no son grados. Son pulsos PWM en microsegundos. Se ajustan en terreno hasta que la mecanica abra y cierre correctamente.

### Bateria

- `battery.adc_pin`: pin ADC usado para medir bateria.
- `battery.divider_ratio`: relacion del divisor resistivo.
- `battery.vref`: referencia ADC.
- `battery.samples`: muestras para promedio.
- `battery.empty_v`: voltaje considerado 0%.
- `battery.full_v`: voltaje considerado 100%.
- `battery.low_hysteresis_v`: margen para limpiar alerta de bateria baja.
- `thresholds.low_battery_v`: umbral para evento `battery_low`.

Si no existe medicion de bateria, el firmware lo informa como telemetria faltante.

### WebSocket

- `ws_enabled`: habilita recepcion de comandos en tiempo real mediante ActionCable.
- `ws_debug`: nivel de traza del canal WebSocket. `1` muestra eventos operacionales; `2` agrega frames, payloads y diagnostico detallado.
- `runtime.ws_pull_max`: cantidad maxima de comandos extraidos desde WebSocket por ciclo de loop.
- `runtime.ws_queue_max`: capacidad maxima de la cola interna de comandos recibidos por WebSocket.
- `runtime.ws_buffer_max_bytes`: limite del buffer de recepcion WebSocket. Si se supera, el canal se cierra para forzar reconexion limpia.
- `runtime.ws_reconnect_delay_ms`: espera entre intentos de reconexion.
- `runtime.ws_idle_timeout_ms`: tiempo maximo sin recibir datos antes de considerar el canal no saludable.
- `runtime.ws_confirm_timeout_ms`: tiempo maximo esperando `confirm_subscription` despues del handshake.
- `runtime.ws_reconnect_fail_reset_threshold`: numero de reconexiones fallidas antes de ejecutar recuperacion de conectividad.
- `runtime.ws_reconnect_fail_modem_reset_threshold`: numero de fallas acumuladas antes de reiniciar el modem SIM7080.
- `runtime.ws_down_heartbeat_sec`: intervalo alternativo de heartbeat cuando el WebSocket esta caido. `0` lo deshabilita.
- `runtime.ws_identifier_include_device_id`: incluye `device_id` dentro del identificador de suscripcion si el backend lo requiere.
- `runtime.ws_token_in_query`: agrega el token como query string en `/cable`. En la integracion validada, esto permite autenticacion correcta de ActionCable.

### Runtime Y Recuperacion

- `runtime.max_command_queue`: capacidad maxima de la cola RAM de comandos antes de descartar entradas nuevas.
- `runtime.loop_sleep_ms`: pausa normal del loop principal. Impacta latencia de comandos, consumo y uso de CPU.
- `runtime.loop_error_backoff_ms`: pausa aplicada despues de errores recuperables para evitar ciclos agresivos de reintento.
- `runtime.supervisor_restart_delay_ms`: espera antes de reiniciar una sesion completa del firmware tras una excepcion controlada.
- `runtime.supervisor_catch_base_exceptions`: si esta en `true`, el supervisor captura errores severos y reinicia sesion. Para pruebas remotas puede ponerse en `false` para permitir detener con interrupcion.
- `runtime.status_log_interval_sec`: frecuencia del resumen operacional `[state]`.
- `runtime.gc_collect_interval_sec`: frecuencia de ejecucion de `gc.collect()` para reducir fragmentacion y presion de memoria.
- `runtime.gc_log_free`: imprime memoria libre/asignada si la plataforma MicroPython lo soporta.
- `runtime.heartbeat_failure_retry_sec`: intervalo temporal reducido usado despues de un heartbeat fallido.
- `runtime.offline_after_heartbeat_failures`: cantidad de heartbeats fallidos consecutivos antes de reportar `device_offline`.
- `runtime.heartbeat_on_startup`: habilita heartbeat inicial obligatorio. En produccion debe mantenerse `true`.
- `runtime.heartbeat_effective_max_sec`: limite superior efectivo del heartbeat local. `0` mantiene la politica normal configurada.
- `runtime.http_open_timeout_ms`: timeout maximo para apertura de socket HTTP/TLS mediante `CAOPEN`.
- `runtime.http_read_timeout_ms`: timeout maximo para lectura de respuesta HTTP mediante `CARECV`.
- `runtime.healthcheck_on_startup`: ejecuta `GET /api/v1/health` antes del flujo operativo.
- `runtime.healthcheck_timeout_fail_open`: permite continuar si el healthcheck falla por timeout o red.
- `runtime.tamper_repeat_suppress_ms`: ventana de supresion de alarmas de tamper repetidas por rebote o apertura sostenida.
- `runtime.power_profile`: perfil operativo. Define criterios de consumo/latencia esperados por configuracion.
- `runtime.processed_id_cache_size`: cantidad de IDs de comando conservados para evitar reprocesamiento duplicado.
- `runtime.debug_operational_enabled`: habilita trazas de API, comandos, eventos, ACK, sensor y estado.
- `runtime.debug_modem_enabled`: habilita trazas de bajo nivel del modem. Debe estar `false` en operacion normal.

### Persistencia Local

- `runtime.journal_enabled`: habilita journal JSONL para comandos pendientes o recuperables.
- `runtime.persist_ws_commands`: si esta activo, persiste comandos recibidos por WebSocket antes de ejecutarlos. En operacion normal puede quedar `false` para reducir escritura flash.
- `runtime.journal_pull_max`: cantidad maxima de comandos leidos desde journal por ciclo.
- `runtime.journal_max_bytes`: tamano maximo del journal antes de rechazar nuevos registros para proteger memoria/flash.
- `runtime.journal_state_save_every`: frecuencia de guardado de offset/estado del journal.
- `runtime.journal_compact_min_bytes`: tamano minimo antes de evaluar compactacion.
- `runtime.journal_compact_ratio_pct`: porcentaje de avance requerido para compactar el journal.
- `runtime.legacy_import_batch`: cantidad de comandos importados por ciclo desde formato legacy. `0` deshabilita importacion automatica.
- `runtime.outbox_flush_max`: cantidad maxima de eventos pendientes enviados por ciclo.
- `runtime.outbox_max_bytes`: tamano maximo del outbox antes de rechazar nuevos eventos pendientes.
- `files.token`: ruta local donde se conserva `device_id` y `auth_token`.
- `files.outbox`: ruta del outbox JSONL de eventos pendientes.
- `files.outbox_state`: ruta del estado de avance del outbox.
- `files.commands_journal`: ruta del journal JSONL de comandos.
- `files.commands_state`: ruta del estado de avance del journal.
- `files.commands_dead_letter`: ruta de comandos/eventos descartados por error no recuperable.
- `files.commands_legacy_inbox`: ruta legacy usada solo para compatibilidad o migracion controlada.

### Reinicio Preventivo

- `runtime.scheduled_reboot_enabled`: habilita reinicio preventivo programado. Por defecto debe quedar `false` hasta aprobacion operacional.
- `runtime.scheduled_reboot_interval_sec`: uptime minimo requerido para ejecutar el reinicio preventivo.
- `runtime.scheduled_reboot_only_when_idle`: si esta activo, reinicia solo cuando no hay comandos pendientes y la compuerta no esta en un estado operativo inseguro.
- `runtime.scheduled_reboot_min_uptime_sec`: evita reinicios muy tempranos despues de energizar el dispositivo.
- `runtime.scheduled_reboot_send_heartbeat`: intenta enviar heartbeat antes del reinicio preventivo para dejar trazabilidad en plataforma.

### Hardware

- `hardware.uart_port`: puerto UART de la Raspberry Pi Pico usado para comunicar con el SIM7080.
- `hardware.baud`: velocidad UART del modem.
- `hardware.led_pin`: pin usado como indicador local si la placa lo expone.
- `hardware.pwr_en_pin`: pin conectado a la habilitacion/encendido del modem.
- `hardware.uart_tx_pin`: GPIO TX asignado al UART.
- `hardware.uart_rx_pin`: GPIO RX asignado al UART.

### Debug

- `debug`: nivel global de diagnostico del firmware.
- `debug=1`: traza operacional recomendada.
- `debug=2`: diagnostico profundo.
- `ws_debug=1`: traza operacional WebSocket recomendada.
- `ws_debug=2`: incluye frames, bytes crudos y diagnostico detallado de ActionCable.
- `runtime.debug_operational_enabled=true`: muestra API, comandos, ACK, sensor, eventos y resumen de estado.
- `runtime.debug_modem_enabled=false`: evita imprimir comandos AT en operacion normal.

Resumen esperado:

```text
[state] actuator_state= ... door_state= ... security_state= ... ws= ... last_hb= ... next_hb= ...
```

## update_config

El comando remoto `update_config` permite modificar:

- `heartbeat_interval`
- `low_battery_threshold`
- `allow_snapshot`
- parametros validados de `gpio`
- `power_profile`

No se permite modificar libremente todos los parametros internos, para evitar que una configuracion remota deje el dispositivo sin heartbeat, sin recuperacion o sin trazabilidad.

## Operacion Esperada

En condiciones normales se debe observar:

```text
[boot] ...
[claim] ok device_id= ...
[api>>] POST /api/v1/devices/{id}/heartbeat ...
[api<<] POST /api/v1/devices/{id}/heartbeat status= 200 op= heartbeat
[heartbeat] startup status= 200 next_pull_sec= ...
[ws] conectando wss://infrakey.fasttrack.cloud:443/cable
[ws] suscripcion confirmada
[state] actuator_state= ... door_state= ... security_state= ... ws= ready ...
```

Durante una apertura autorizada:

1. Llega `open_actuator`.
2. Se emite `authorization_request`.
3. Se mueve el actuador.
4. Se emite `authorization_granted`.
5. Cuando el sensor confirma apertura, se emite `device_opened`.

Durante un cierre:

1. Llega `close_actuator`.
2. Se mueve el actuador.
3. El estado queda `locking_pending`.
4. Cuando el sensor confirma cerrado, se emite `device_closed`.
5. El estado vuelve a `locked`.

Si el sensor abre sin autorizacion, se emiten `tamper_alert` y `unauthorized_access`.

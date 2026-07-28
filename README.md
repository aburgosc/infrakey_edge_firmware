# Infrakey Edge Firmware

Firmware embebido para Raspberry Pi Pico + SIM7080, diseñado para integrarse con la plataforma Infrakey mediante API REST y WebSocket.

Este documento describe el proyecto, su arquitectura, su operación y la forma de ejecutarlo y validarlo. La planificación de cierre y la validación en terreno viven en documentos separados dentro de `docs/`.

## Objetivo

El firmware permite que un dispositivo físico opere como nodo conectado dentro del ecosistema Infrakey, con capacidades de:

- registro e identificación del dispositivo
- telemetría periódica
- recepción de comandos remotos
- control de actuador local
- emisión de eventos operativos
- recuperación básica ante reinicios y fallas transitorias

## Plataforma objetivo

La base está desarrollada para `MicroPython` sobre Raspberry Pi Pico o plataforma equivalente compatible con:

- `machine`
- `utime`
- `ujson`
- `uhashlib`
- `ubinascii`

No es una aplicación Python de escritorio ni un servicio Linux.

## Componentes principales

### Runtime

[main.py](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/main.py:1) coordina:

- carga de configuración
- bringup del módem
- `health`
- `claim`
- `heartbeat` inicial
- canal WebSocket
- loop principal
- recuperación y compactación de colas persistentes

### Hardware y módem

- [sim7080mini/hal.py](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/sim7080mini/hal.py:1)
- [sim7080mini/modem.py](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/sim7080mini/modem.py:1)

### Cliente API

- [sim7080mini/httpclient.py](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/sim7080mini/httpclient.py:1)
- [sim7080mini/infrakey.py](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/sim7080mini/infrakey.py:1)

### WebSocket

- [sim7080mini/ws_feeder.py](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/sim7080mini/ws_feeder.py:1)

### Lógica funcional

- [sim7080mini/handlers.py](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/sim7080mini/handlers.py:1)
- [sim7080mini/actuator.py](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/sim7080mini/actuator.py:1)

### Pipeline de comandos

- [sim7080mini/commandfeeder.py](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/sim7080mini/commandfeeder.py:1)

## Integración con backend

### Endpoints REST

El firmware consume:

- `POST /api/v1/devices/claim`
- `GET /api/v1/health`
- `POST /api/v1/devices/{device_id}/heartbeat`
- `POST /api/v1/devices/{device_id}/events`
- `POST /api/v1/devices/{device_id}/events/snapshot`
- `POST /api/v1/devices/{device_id}/commands/{id}/ack`

La referencia funcional del contrato está en [docs/Dcoumentacion_API_V1.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Dcoumentacion_API_V1.md:1).

### WebSocket

El canal en tiempo real usa:

- `wss://api.infrakey.fasttrack.cloud/cable`
- suscripción a `DeviceCommandsChannel`
- autenticación `Authorization: Bearer <auth_token>`

## Comandos implementados

### Comandos del contrato API

- `open_actuator`
- `close_actuator`
- `update_config`
- `ping`
- `snapshot`

### Compatibilidades y soporte interno

- `pulse_actuator`
- `instantanea` como alias de `snapshot`
- `test_event` para validación controlada

## Eventos implementados

### Eventos del contrato API

- `tamper_alert`
- `battery_low`
- `device_opened`
- `device_closed`
- `authorization_granted`
- `authorization_request`
- `snapshot`
- `device_offline`
- `unauthorized_access`

### Sensor de apertura y seguridad

El contacto de puerta se conecta a `gpio.sensor_pin`. El firmware aplica
debounce y distingue el origen de cada apertura:

- `open_actuator` habilita una sola apertura durante
  `sensor_authorized_open_ms`; esa transición no genera alarma.
- El cierre físico consume cualquier permiso pendiente.
- Una apertura posterior sin una orden vigente genera `tamper_alert` y
  `unauthorized_access`. Si no hay red, ambos eventos quedan en el outbox.
- Si el equipo arranca con la puerta abierta, genera una alerta después de
  `sensor_boot_grace_ms` cuando `sensor_alert_if_open_on_boot` está activo.

La polaridad real debe comprobarse en la placa con `probe_sensor.py`. Los
valores `sensor_open_is` y `sensor_pull` no deben asumirse sin esa medición.

## Arquitectura de comandos

El producto usa una arquitectura `RAM + JSONL`:

- la ejecución normal ocurre desde una cola en RAM
- WebSocket es la fuente principal de comandos en tiempo real
- `JSONL` se usa como persistencia mínima y mecanismo de recovery
- existe `dead-letter` para entradas inválidas
- existe estado `inflight` para recuperación tras reinicio
- existe deduplicación por `cmd_id`

Archivos relevantes:

- [commands_queue.jsonl](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/commands_queue.jsonl:1)
- `commands_state.json`
- [commands_dead.jsonl](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/commands_dead.jsonl:1)
- [outbox.jsonl](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/outbox.jsonl:1)
- `outbox_state.json`

## Flujo operativo

El flujo normal es:

1. carga de configuración
2. arranque del módem
3. `health`
4. `claim` o carga de credenciales persistidas
5. `heartbeat` inicial
6. apertura del canal WebSocket si está habilitado
7. ejecución del loop principal
8. recepción de comandos
9. emisión de eventos y tareas periódicas

## Configuración

La configuración principal vive en [device_config.json](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/device_config.json:1).

Campos importantes:

- `host`
- `port`
- `nb_band`
- `apn_fallback`
- `user_agent`
- `fw`
- `model`
- `latitude`
- `longitude`
- `heartbeat_interval_sec`
- `ws_enabled`
- `gpio`
- `thresholds`
- `battery`
- `gps`
- `runtime`
- `files`

Los parámetros de seguridad del sensor están dentro de `gpio`:
`sensor_pin`, `sensor_open_is`, `sensor_pull`, `sensor_debounce_ms`,
`sensor_authorized_open_ms`, `sensor_alert_if_open_on_boot` y
`sensor_boot_grace_ms`.

Ejemplo de perfil de terreno:

- [docs/device_config.terreno.example.json](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/device_config.terreno.example.json:1)

## Persistencia local

- [token.json](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/token.json:1): credenciales persistidas
- `device_config.json`: configuración operativa
- `commands_queue.jsonl`: journal de comandos
- `commands_state.json`: cursor, `inflight`, deduplicación
- `commands_dead.jsonl`: entradas inválidas
- `outbox.jsonl`: eventos diferidos
- `outbox_state.json`: estado del outbox

## Ejecución

### En hardware

El firmware está pensado para ejecutarse como:

```python
main.py
```

### En entorno local con mock

```powershell
$env:SIM7080_USE_MOCK="1"
python main.py
```

Esto sirve para validar lógica y flujo general, no reemplaza:

- hardware real
- red real
- backend real

## Validaciones automáticas

### Flujo funcional

```powershell
python tools/validate_firmware_mock_flow.py
```

### Pipeline de comandos

```powershell
python tools/validate_command_pipeline.py
```

### Transporte mock REST

```powershell
python tools/validate_mock_transport.py
```

La evidencia de estas validaciones está documentada en:

- [docs/Validacion_Firmware_Mock_Flow.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Validacion_Firmware_Mock_Flow.md:1)
- [docs/Validacion_Command_Pipeline.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Validacion_Command_Pipeline.md:1)
- [docs/Validacion_Mock_Transport.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Validacion_Mock_Transport.md:1)

## Qué esperar del runtime

Trazas típicas:

- `[boot]`
- `[health]`
- `[claim]`
- `[heartbeat] startup`
- `[ws] socket abierto; esperando confirm_subscription`
- `[ws] suscripcion confirmada`
- `HB: <status> next: <segundos> failures: <n> offline_queued: <bool>`

Comportamientos esperados:

- recuperación de credenciales ante `401`
- clamp de `next_pull_sec`
- reconexión WS ante canal no saludable
- eventos diferidos vía outbox
- recuperación de `inflight` tras reinicio

## Estructura relevante del repositorio

```text
.
|-- main.py
|-- device_config.json
|-- token.json
|-- commands_queue.jsonl
|-- commands_dead.jsonl
|-- outbox.jsonl
|-- docs/
|-- sim7080mini/
`-- tools/
```

## Documentación relacionada

- [docs/Dcoumentacion_API_V1.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Dcoumentacion_API_V1.md:1)
- [docs/Arquitectura_Comandos_RAM_JSONL.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Arquitectura_Comandos_RAM_JSONL.md:1)
- [docs/Matriz_Pruebas_Firmware.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Matriz_Pruebas_Firmware.md:1)
- [docs/PLAN_VALIDATION.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/PLAN_VALIDATION.md:1)
- [docs/Guia_Observacion_Consola.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Guia_Observacion_Consola.md:1)

## Nota final

Este `README` documenta el proyecto y su operación. La validación final en hardware y la evidencia de aceptación deben seguir la pauta definida en `docs/PLAN_VALIDATION.md`.

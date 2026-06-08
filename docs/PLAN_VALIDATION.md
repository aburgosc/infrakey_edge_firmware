# PLAN VALIDATION

Pauta ordenada para validar el firmware final con hardware real, backend real y WebSocket real.

Este documento define:

- prerequisitos
- secuencia de ejecución
- evidencia a capturar
- criterios de aprobación

## 1. Objetivo

Confirmar que el firmware:

- arranca correctamente en hardware real
- se integra correctamente con la API documentada
- recibe y ejecuta comandos del contrato
- emite eventos del contrato con payload coherente
- mantiene estabilidad operativa ante fallas transitorias

## 2. Alcance de validación

### REST

- `health`
- `claim`
- `heartbeat`
- `events`
- `snapshot`
- `ack`

### WebSocket

- handshake
- `confirm_subscription`
- recepción de comandos
- `ACK` por WS si aplica
- reconexión

### Hardware

- actuador
- sensor de estado
- tamper
- alimentación

### Persistencia y recovery

- `token.json`
- `commands_queue.jsonl`
- `commands_state.json`
- `outbox.jsonl`
- `outbox_state.json`

## 3. Prerrequisitos

### Hardware

- Raspberry Pi Pico con firmware cargado
- módulo SIM7080 operativo
- SIM activa
- antena conectada
- alimentación estable
- actuador conectado
- sensor de estado conectado
- tamper conectado si aplica

### Configuración

- [device_config.json](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/device_config.json:1) revisado
- acceso al backend real
- acceso al canal que envía comandos
- consola serial o equivalente

### Documentos a mano

- [docs/Dcoumentacion_API_V1.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Dcoumentacion_API_V1.md:1)
- [docs/Matriz_Evidencia_Integracion_Real.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Matriz_Evidencia_Integracion_Real.md:1)
- [docs/Guia_Observacion_Consola.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Guia_Observacion_Consola.md:1)
- [docs/Checklist_Cierre_Firmware.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Checklist_Cierre_Firmware.md:1)

## 4. Orden recomendado

1. Pre-flight
2. Arranque
3. REST base
4. WebSocket
5. Comandos del contrato
6. Eventos del contrato
7. Temporizadores y fallback
8. Recovery y persistencia
9. Telemetría
10. Consolidación de evidencia

## 5. Fase 0: Pre-flight

Verificar antes de energizar:

- cableado
- fuente
- SIM
- antena
- configuración cargada
- visibilidad de consola
- acceso al backend

### Aprobación

- entorno listo
- sin bloqueos externos evidentes

## 6. Fase 1: Arranque

### Pasos

1. energizar el equipo
2. abrir consola
3. observar bringup
4. confirmar que el loop quede corriendo

### Esperado en consola

- `[boot]`
- `[health]`
- `[claim]`
- `[heartbeat] startup`

### Evidencia

- log de arranque
- `device_id`
- hora de inicio

### Aprobación

- arranque completo
- sin crash

## 7. Fase 2: REST base

### Casos

#### 2.1 `health`

- confirmar respuesta correcta

#### 2.2 `claim`

- confirmar obtención o recuperación de token

#### 2.3 `heartbeat`

- confirmar `heartbeat` aceptado
- confirmar aplicación de `next_pull_sec` si aparece

#### 2.4 `ack`

- confirmar cierre de comando en backend

### Evidencia

- status HTTP
- payload observado
- timestamps de backend

### Aprobación

- endpoints básicos operativos

## 8. Fase 3: WebSocket

### Casos

1. apertura del socket
2. `confirm_subscription`
3. recepción de comando
4. trazabilidad de `ACK`
5. reconexión tras corte controlado

### Esperado en consola

- `socket abierto; esperando confirm_subscription`
- `suscripcion confirmada`
- trazas del comando recibido

### Evidencia

- log de suscripción
- comando recibido
- `ACK` asociado

### Aprobación

- no se procesan comandos antes de la suscripción
- el canal se recupera si se induce caída

## 9. Fase 4: Comandos del contrato

Ejecutar estos comandos:

1. `ping`
2. `open_actuator`
3. `close_actuator`
4. `snapshot`
5. `update_config`

### Opcional de compatibilidad interna

- `pulse_actuator`

Solo correrlo si el backend real lo soporta o si se quiere validar extensión interna del firmware.

### Para cada comando registrar

- timestamp de envío
- timestamp de recepción
- resultado local
- `ACK` HTTP
- `ACK` WS si aplica
- efecto visible

### Aprobación

- ejecución o rechazo coherente
- sin pérdida silenciosa

## 10. Fase 5: Eventos del contrato

Validar:

1. `tamper_alert`
2. `unauthorized_access`
3. `battery_low`
4. `authorization_request`
5. `authorization_granted`
6. `device_offline`
7. `device_opened`
8. `device_closed`

### Método

- inducir condición física o funcional
- observar consola
- observar backend
- correlacionar evento con la acción real

### Evidencia

- `event_id`
- severidad observada
- status backend
- timestamp backend

### Aprobación

- el evento correcto aparece
- la severidad es coherente con la API

## 11. Fase 6: Temporizadores y fallback

### Casos

#### 6.1 Heartbeat local

- validar periodicidad observada

#### 6.2 `next_pull_sec`

- validar aplicación y clamp

#### 6.3 Falla temporal de red

- inducir falla
- confirmar continuidad del loop

#### 6.4 Recuperación

- restaurar conectividad
- validar recuperación de heartbeat
- validar flush de outbox

### Aprobación

- el proceso no cae
- el recovery es consistente

## 12. Fase 7: Recovery y persistencia

### Casos

#### 7.1 Reinicio controlado

- reiniciar
- confirmar recuperación del runtime

#### 7.2 Comando persistido

- inducir reinicio cercano a un comando persistido
- validar tratamiento de `inflight`

#### 7.3 Evento diferido

- forzar evento con backend inaccesible
- restaurar conectividad
- validar flush posterior

### Evidencia

- estado de archivos persistentes
- trazas de recovery
- resultado final

### Aprobación

- sin corrupción visible
- recovery coherente

## 13. Fase 8: Telemetría

Validar:

- `battery_v`
- `battery_pct`
- `snapshot`
- `heartbeat`
- coordenadas
- `gps_source`
- `telemetry_missing` si aplica

### Aprobación

- payload coherente con el entorno
- si falta dato real, el firmware no inventa un valor engañoso

## 14. Fase 9: Consumo

Si existe instrumentación:

- registrar consumo en reposo
- registrar consumo con WS activo
- registrar consumo durante heartbeat

Si no existe instrumentación:

- dejarlo marcado como pendiente de medición formal

## 15. Evidencia mínima obligatoria

- log de arranque
- evidencia de `health`
- evidencia de `claim`
- evidencia de `heartbeat`
- evidencia de `confirm_subscription`
- evidencia de `ping`
- evidencia de `open_actuator`
- evidencia de `close_actuator`
- evidencia de `snapshot`
- evidencia de al menos un evento crítico
- evidencia de recovery ante falla temporal
- matriz completada

## 16. Criterios de aprobación final

La validación se considera aprobada si:

- REST funciona con backend real
- WS funciona con backend real
- los comandos del contrato ejecutan correctamente
- los eventos del contrato quedan observables
- el loop se mantiene estable
- recovery y persistencia se comportan correctamente

## 17. Criterios de pausa o rechazo

Detener si aparece alguno de estos casos:

- reinicio repetitivo
- pérdida persistente de conectividad sin recovery
- comandos sin trazabilidad
- eventos incorrectos o invertidos
- actuación física insegura
- corrupción evidente de archivos de estado

## 18. Salida final

Al terminar deben quedar actualizados:

- [docs/Matriz_Evidencia_Integracion_Real.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Matriz_Evidencia_Integracion_Real.md:1)
- [docs/Checklist_Cierre_Firmware.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Checklist_Cierre_Firmware.md:1)
- [docs/Informe_Conformidad_Firmware.md](C:/Proyectos/Raspberry%20Pi/Proyecto%20Braulio/Testing-APIS/proyectofinal/docs/Informe_Conformidad_Firmware.md:1)

## 19. Resultado posible

### Caso A

Validado sin observaciones mayores.

### Caso B

Validado con observaciones menores y feedback para ajuste acotado.

### Caso C

Validación rechazada con hallazgo bloqueante reproducible.

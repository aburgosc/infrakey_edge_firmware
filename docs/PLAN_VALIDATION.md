# PLAN VALIDATION

Pauta simple para validar el firmware con hardware real y backend real.

## Objetivo

Confirmar que el dispositivo:

- arranca correctamente
- se conecta a la red
- se registra en Infrakey
- envia heartbeat inicial y periodico
- mantiene WebSocket operativo
- recibe comandos remotos
- mueve el actuador
- detecta apertura y cierre fisico
- reporta eventos correctos
- se recupera ante fallas transitorias

## Prerrequisitos

Antes de iniciar:

- SIM activa y con cobertura.
- Antenas LTE y GNSS conectadas.
- Alimentacion estable.
- Actuador conectado.
- Sensor magnetico conectado.
- Configuracion de terreno cargada usando `device_config.terreno.example.json` como referencia.
- Acceso a consola serial.
- Acceso a la plataforma Infrakey para observar estado, eventos y comandos.

## 1. Arranque

Pasos:

1. Energizar el dispositivo.
2. Abrir la consola serial.
3. Esperar el arranque completo.

Esperado:

- aparece `[boot]`
- el modem responde
- se obtiene o reutiliza `device_id`
- se envia heartbeat inicial
- no hay crash

Criterio de aprobacion:

- el dispositivo queda en ejecucion continua y muestra resumen `[state]`.

## 2. Red Y API

Validar:

- el dispositivo logra conectarse a NB-IoT
- el APN queda activo
- el heartbeat inicial responde correctamente
- los heartbeats periodicos siguen ejecutandose
- si hay una falla HTTP, el firmware no se detiene y reintenta

Criterio de aprobacion:

- la plataforma muestra ultimo ping actualizado.
- la consola no vuelve al prompt por una falla silenciosa.

## 3. WebSocket

Validar:

1. El dispositivo abre el canal WebSocket.
2. La suscripcion queda confirmada.
3. Se observan pings de ActionCable.
4. Se recibe al menos un comando remoto.
5. Si el canal cae, el firmware intenta reconectar.

Esperado en consola:

```text
[ws] conectando ...
[ws] suscripcion confirmada
[ws] actioncable ping
```

Criterio de aprobacion:

- el dispositivo puede recibir comandos sin reinicio manual.

## 4. Comandos

Probar desde la plataforma:

1. `ping`
2. `open_actuator`
3. `close_actuator`
4. `snapshot`
5. `update_config`

Para cada comando validar:

- llega por consola
- se ejecuta o rechaza con razon clara
- se envia ACK
- la plataforma refleja el resultado esperado

Criterio de aprobacion:

- todos los comandos documentados responden sin bloquear el loop.

## 5. Apertura Autorizada

Pasos:

1. Dejar la compuerta cerrada.
2. Enviar `open_actuator`.
3. Confirmar que el actuador se mueve.
4. Abrir fisicamente la compuerta.

Esperado:

- se emite `authorization_request`
- se emite `authorization_granted`
- al abrir el sensor se emite `device_opened`
- no se emite `tamper_alert`
- no se emite `unauthorized_access`

Criterio de aprobacion:

- una apertura remota autorizada nunca genera falsa alarma.

## 6. Cierre Remoto O Manual

Pasos:

1. Con la compuerta abierta, enviar `close_actuator`.
2. Confirmar que el actuador se mueve.
3. Cerrar fisicamente la compuerta o confirmar que el mecanismo la cerro.

Esperado:

- el estado pasa a `locking_pending`
- no se emite `device_closed` solo por ejecutar el comando
- cuando el sensor confirma cerrado, se emite `device_closed`
- el estado vuelve a `locked`

Criterio de aprobacion:

- el cierre se confirma solo por sensor fisico.

## 7. Apertura No Autorizada

Pasos:

1. Dejar el dispositivo en estado cerrado y bloqueado.
2. Abrir fisicamente el sensor sin enviar comando remoto.

Esperado:

- se emite `tamper_alert`
- se emite `unauthorized_access`
- no se duplica la alerta por rebote

Criterio de aprobacion:

- una apertura forzada queda registrada como evento de seguridad.

## 8. Arranque Con Sensor Abierto

Pasos:

1. Dejar el sensor en estado abierto.
2. Reiniciar el dispositivo.

Esperado:

- se detecta condicion abierta al iniciar
- se reporta alerta diferenciada de arranque anomalo
- no se generan eventos duplicados continuamente

Criterio de aprobacion:

- el sistema distingue arranque abierto de una apertura forzada posterior.

## 9. GPS

Validar:

- el dispositivo intenta leer GNSS real
- heartbeat y snapshot incluyen `latitude` y `longitude` cuando hay lectura o cache real
- si no hay GPS real, no se inventan coordenadas estaticas cuando la configuracion lo prohibe

Criterio de aprobacion:

- la plataforma muestra ubicacion cuando hay coordenadas reales disponibles.

## 10. Heartbeat Y Online

Validar:

- se envia heartbeat al arranque
- se envia heartbeat periodico
- `update_config` puede cambiar el intervalo permitido
- el dispositivo no queda offline si WebSocket cae pero el heartbeat sigue funcionando
- si el heartbeat falla, se reintenta con el intervalo de recuperacion

Criterio de aprobacion:

- el ultimo ping en plataforma se mantiene actualizado.

## 11. Recuperacion

Probar fallas controladas:

- perdida temporal de senal
- caida temporal del WebSocket
- respuesta HTTP fallida
- reinicio controlado del dispositivo

Esperado:

- el firmware imprime la causa
- no queda bloqueado sin traza
- reintenta conexion
- vuelve a operar cuando la red se recupera

Criterio de aprobacion:

- no requiere intervencion manual para volver a operar tras fallas transitorias.

## 12. Criterios Finales De Aprobacion

El sistema se considera validado si:

- arranca de forma estable
- reporta heartbeat inicial
- mantiene heartbeat periodico
- recibe comandos por WebSocket
- ejecuta apertura y cierre
- confirma apertura/cierre por sensor
- reporta tamper real
- no genera tamper falso en apertura autorizada
- reporta ubicacion real cuando GNSS esta disponible
- recupera WebSocket y API ante fallas transitorias
- mantiene trazabilidad clara por consola

## 13. Criterios De Rechazo

Detener la validacion si ocurre:

- actuador se mueve de forma insegura
- sensor reporta estados invertidos
- heartbeat deja de ejecutarse
- WebSocket cae y nunca intenta reconectar
- una apertura autorizada genera `unauthorized_access`
- una apertura forzada no genera alerta
- el firmware vuelve al prompt sin explicar la falla
- el dispositivo requiere reinicio manual para recuperar una falla transitoria

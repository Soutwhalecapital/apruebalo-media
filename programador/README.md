# Programador de Historias (Pedido 9, fase 1)

Publica solo las Historias de Instagram de Apruébalo con la API de Meta, desde GitHub Actions, sin Metricool.

**Estado actual: en pausa.** No existe `programador/activo.txt`, así que el flujo programado corre pero no publica nada. Se activa después de la prueba real contigo.

## Qué hace

Cada 30 minutos, entre las 12:00 y las 21:59 hora de Chile, el flujo `.github/workflows/programador.yml` corre `programador/publicar.py`, que:

1. Lee las Historias de todos los `tanda-N/historias-programables.csv` (hoy tanda-1, tanda-2 y tanda-3) desde la fecha de `programador/desde.txt`.
2. Deja **una sola Historia por día**: la de la tanda más nueva (ver más abajo).
3. Toma las que tengan su hora de publicación dentro de las **últimas 6 horas** y que el registro no bloquee. La hora es la de la columna `hora` del CSV si existe (HH:MM, hora de Chile); si no, las **18:00**. En la práctica, la Historia del día sale a las 18:07.
4. Usa la versión **JPEG** de cada imagen (la misma URL con `.jpg`), porque la API de Instagram solo acepta JPEG. Antes de crear nada revisa que el JPEG responda 200.
5. Con la Graph API (versión fijada en `GRAPH_VERSION`, hoy v27.0) obtiene la cuenta de Instagram de la página 106944895827994, crea el contenedor `STORIES`, consulta `status_code` cada 30 segundos hasta `FINISHED` (máximo 5 minutos) y publica con `media_publish`.
6. Anota cada intento en `programador/registro.csv` y el flujo hace commit y push del registro.
7. Si algo falla, deja el error en el registro y termina con error. Así GitHub te manda el correo de falla.

## Archivos

| Archivo | Para qué |
| --- | --- |
| `programador/publicar.py` | El script. Solo usa la biblioteca estándar de Python. |
| `programador/desde.txt` | Fecha desde la que publica (AAAA-MM-DD). Hoy: 2026-11-16. |
| `programador/activo.txt` | Interruptor. Si existe, el flujo programado publica; si no, está en pausa. El contenido da lo mismo. |
| `programador/registro.csv` | Un renglón por intento: `ID;programada;publicada;atraso_min;media_id;estado;error`. |
| `programador/test_publicar.py` | Pruebas. |
| `.github/workflows/programador.yml` | El flujo de GitHub Actions. |

## Cuándo corre

`cron: '7,37 12-21 * * *'` con `timezone: 'America/Santiago'`: a las 12:07, 12:37, 13:07… hasta las 21:37, hora de Chile. GitHub acepta `timezone` con nombres IANA y sigue solo el cambio de horario de Chile (UTC-3 en verano, UTC-4 en invierno). Se evita el minuto 0 porque GitHub atrasa las ejecuciones al empezar cada hora.

GitHub puede atrasar una corrida programada unos minutos. No importa: la ventana es de 6 horas.

## La regla de la tanda más nueva

Los CSV se pisan: por ejemplo, tanda-1 y tanda-3 tienen los dos un H-2026-11-17, y tanda-2 y tanda-3 un H-2026-12-02, con imágenes distintas. La regla es: **para cada fecha se usa solo la fila del CSV de la tanda con el número más alto que tenga esa fecha.** Así sale una sola Historia por día.

Con los CSV de hoy, desde el 16-11 quedan 34 Historias: del 16-11 al 13-12 todas salen de tanda-3 (como dice el informe del Pedido 8), y el 16, 19, 22, 26, 28 y 30 de diciembre, de tanda-2.

Si agregas una `tanda-4/historias-programables.csv`, entra sola y manda sobre las anteriores en las fechas que tenga. Las filas cuyo `tipo` no sea Historia se ignoran. Si la tanda ganadora trae dos filas para la misma fecha, el script se detiene con un error claro en vez de adivinar.

## El registro y los reintentos

Cada intento deja un renglón en `programador/registro.csv` con uno de estos estados:

- **publicada**: salió. Nunca se vuelve a publicar.
- **error**: falló **antes** de publicar (falta el token, el JPEG no responde, el contenedor quedó en `ERROR`, Meta rechazó la petición, etc.). No salió nada.
- **incierto**: se llamó a `media_publish` y no hubo respuesta clara (se cortó la red, Meta respondió 5xx). El script pregunta al contenedor: si figura `PUBLISHED`, lo anota como publicada; si no, queda incierto.

Cómo se reintenta:

- Con **un** error, se reintenta **una sola vez**, en la corrida siguiente (30 minutos después), si sigue dentro de la ventana de 6 horas.
- Con **dos** errores, no se insiste más. Como mucho llegan **2 correos de falla por Historia**, no uno cada 30 minutos durante 6 horas.
- Un **incierto** no se reintenta nunca solo, para no publicar dos veces. Revisa la cuenta de Instagram: si la Historia no salió y todavía quieres publicarla, borra ese renglón del registro y el programador la reintentará en la próxima corrida, si sigue dentro de la ventana.
- Un estado que el script no conoce también bloquea: ante la duda, no publica.
- Para forzar un reintento de una Historia con 2 errores, borra sus renglones de error del registro.

El flujo trae siempre la última versión de la rama principal antes de correr y no deja que corran dos a la vez (`concurrency`). Así cada corrida ve lo que anotó la anterior.

## Cómo pausarlo

Cualquiera de estas dos:

- **Borra `programador/activo.txt`** del repositorio. El flujo sigue corriendo, pero termina bien sin publicar nada. Para reanudar, vuelve a crear el archivo (puede ir vacío).
- **Deshabilita el flujo**: en GitHub, Actions > Programador de Historias > botón «…» > Disable workflow (o `gh workflow disable programador.yml`). Para reanudar, Enable workflow.

Ojo: GitHub deshabilita solo los flujos programados de un repositorio público si pasan 60 días sin actividad. Si pasa, vuelve a habilitarlo en Actions.

## Cómo cambiar la fecha de inicio

Edita `programador/desde.txt` y deja una sola fecha AAAA-MM-DD (por ejemplo `2026-11-20`). Las Historias de antes de esa fecha se ignoran. Haz commit y push; la próxima corrida ya la usa.

## Metricool

Mientras exista `programador/activo.txt`, **la tarea de los viernes no programa Historias en Metricool desde la fecha de `desde.txt`**: esas Historias las publica este programador. Si las dos lo hicieran, saldrían dos veces. Si pausas el programador por varios días, vuelve a programar esas Historias en Metricool o mueve `desde.txt`.

## Probar sin publicar (simular)

`--simular` hace todo menos crear y publicar: lista lo que publicaría, revisa los JPEG y, si hay token, valida el acceso a Meta, el id de Instagram y la cuota de publicación. No toca el registro. Sin token termina con error después de listar, con un mensaje claro. Funciona igual exista o no `activo.txt`.

- **En GitHub**: Actions > Programador de Historias > Run workflow, con la casilla «Simular» marcada (viene marcada). O bien `gh workflow run programador.yml -f simular=true`.
- **En el PC** (desde la carpeta del repositorio):

  ```
  python programador/publicar.py --simular
  python programador/publicar.py --simular --ahora 2026-11-16T18:30
  ```

  `--ahora` fija la hora (sin zona, se entiende hora de Chile) y solo se acepta con `--simular`. En Windows, si el script dice que no encuentra la zona horaria, instala `pip install tzdata`.

Si desmarcas «Simular» en Run workflow, la corrida es **real**: publica lo que esté en la ventana aunque no exista `activo.txt`. Sirve para la prueba real; no la uses sin querer publicar.

## El acceso de Meta

El token vive **solo** como secreto del repositorio `META_TOKEN` (Settings > Secrets and variables > Actions). No va en el código ni en ningún archivo. El script lo manda solo en la cabecera `Authorization` a graph.facebook.com y lo borra de todo lo que imprime o anota en el registro.

Permisos que pide la guía de publicación de Meta (Facebook Login): `instagram_basic`, `instagram_content_publish` y `pages_read_engagement`, sobre la página 106944895827994. La simulación con token comprueba justo eso: lee la cuenta de Instagram de la página y su cuota de publicación. Conviene un token que no venza (de usuario del sistema del Business Manager) o uno de larga duración; si vence, las publicaciones fallan con error y llega el correo.

Los correos de falla los manda GitHub a la cuenta que modificó por última vez la línea `cron` del flujo (hoy Soutwhalecapital) y, en las corridas manuales, a quien las lanzó. Revisa que en GitHub > Settings > Notifications > Actions esté activado el aviso de flujos fallidos.

## Sumar después el feed y Facebook

- **Feed de Instagram**: la API de Instagram no programa a futuro, así que va por este mismo programador. Se agrega un CSV (por ejemplo `tanda-N/posts-programables.csv`) con columna de texto, y para cada fila se crea el contenedor con `image_url` y `caption` (sin `media_type` para una imagen sola; carruseles con `is_carousel_item` y `media_type=CAROUSEL`; reels con `media_type=REELS` y `video_url`), se espera `FINISHED` y se publica con `media_publish`. Ojo: la cuenta tiene un límite de publicaciones por API cada 24 horas (`content_publishing_limit`).
- **Facebook**: la API de páginas **sí permite programar a futuro**. Se publica en `/{page-id}/feed` con `published=false` y `scheduled_publish_time` (entre 10 minutos y 30 días adelante), con un token de página con `pages_manage_posts`. Por eso Facebook no necesita correr cada 30 minutos: basta una tarea que programe la semana de una vez (por ejemplo, los viernes) y anote los ids en un registro propio. Para fotos, revisa en la referencia de `/{page-id}/photos` los mismos campos antes de usarlos.

## Pruebas

Desde la raíz del repositorio:

```
python -m unittest discover -s programador -v
```

Cubren la ventana de 6 horas, el horario de verano e invierno de Chile y los dos cambios de hora, `desde.txt`, la columna `hora`, la regla de la tanda más nueva (también con los CSV reales del repositorio), la lectura y escritura del registro, los reintentos, la pausa sin `activo.txt` y que el token no aparezca en la salida ni en el registro. No usan la red ni el token: Meta y GitHub se reemplazan por respuestas falsas.

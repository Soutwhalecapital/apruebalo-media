#!/usr/bin/env python3
"""Programador propio de Apruébalo, fase 1 (Pedido 9): publica Historias de Instagram
con la Graph API de Meta.

Modos:
  --programado  lo usa el flujo programado de GitHub Actions. Si no existe
                programador/activo.txt, no publica nada y termina bien.
  (sin opción)  corrida manual real: publica lo que esté en la ventana, exista o no
                activo.txt (sirve para la prueba real con Andy).
  --simular     hace todo menos crear y publicar: lista lo que publicaría, revisa los
                JPEG y, si hay token, valida el acceso y el id de Instagram. No toca
                el registro. Sin token, falla con código 1 después de listar.
  --ahora       fija la hora (solo con --simular). Sin zona, se entiende hora de Chile.
                Ejemplo: --ahora 2026-11-16T18:30

El acceso de Meta se lee solo de la variable de entorno META_TOKEN. Viaja solo en la
cabecera Authorization de las peticiones a graph.facebook.com y se borra de todo texto
que se imprima o se guarde.

Solo usa la biblioteca estándar de Python 3.9 o superior.
"""
from __future__ import annotations

import argparse
import csv
import http.client
import io
import json
import os
import re
import sys
import time as _time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

# Versión de la Graph API. v27.0 es la vigente: Meta la lanzó el 4-11-2026
# (verificado el 9-11-2026 en developers.facebook.com).
GRAPH_VERSION = "v27.0"
GRAPH_URL = "https://graph.facebook.com/" + GRAPH_VERSION
PAGINA_ID = "106944895827994"

ZONA_CHILE = "America/Santiago"
HORA_POR_DEFECTO = time(18, 0)
VENTANA = timedelta(hours=6)
INTERVALO_ESTADO_S = 30
ESPERA_MAXIMA_S = 5 * 60
MAX_INTENTOS = 2  # el primer intento y un solo reintento (ver README)
TIMEOUT_HTTP_S = 30

ARCHIVO_CSV = "historias-programables.csv"
PATRON_TANDA = re.compile(r"^tanda-(\d+)$")
COLUMNAS_REGISTRO = ["ID", "programada", "publicada", "atraso_min", "media_id", "estado", "error"]
ESTADO_PUBLICADA = "publicada"
ESTADO_ERROR = "error"
ESTADO_INCIERTO = "incierto"


class ErrorConfig(Exception):
    """Falta o está mal un archivo de configuración (CSV, desde.txt, registro)."""


class ErrorGraph(Exception):
    """Falla al hablar con la Graph API.

    rechazo=True significa que Meta respondió con un error explícito (HTTP 4xx con
    JSON de error): la petición no tuvo efecto. rechazo=False significa que no hubo
    respuesta clara (red, tiempo agotado, HTTP 5xx), así que no se sabe si tuvo efecto.
    """

    def __init__(self, mensaje: str, rechazo: bool):
        super().__init__(mensaje)
        self.rechazo = rechazo


class ErrorPublicacion(Exception):
    """Falla al publicar una Historia. incierto=True: puede que sí se haya publicado."""

    def __init__(self, mensaje: str, incierto: bool = False):
        super().__init__(mensaje)
        self.incierto = incierto


# ---------------------------------------------------------------- secretos y salida

_SECRETOS: list[str] = []


def registrar_secreto(valor: str) -> None:
    """Anota un valor que nunca debe aparecer en lo que se imprime o se guarda."""
    if not valor:
        return
    for variante in (valor, urllib.parse.quote(valor, safe=""), urllib.parse.quote_plus(valor)):
        if variante and variante not in _SECRETOS:
            _SECRETOS.append(variante)
    _SECRETOS.sort(key=len, reverse=True)


def limpiar(texto: object) -> str:
    """Devuelve el texto sin ningún secreto registrado."""
    texto = str(texto)
    for secreto in _SECRETOS:
        texto = texto.replace(secreto, "***")
    return texto


def log(texto: object = "") -> None:
    print(limpiar(texto), flush=True)


# ---------------------------------------------------------------- fechas y horas


def zona_chile():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(ZONA_CHILE)
    except Exception as e:  # sin base de zonas horarias (p. ej. Windows sin tzdata)
        raise ErrorConfig(
            f"No pude cargar la zona horaria {ZONA_CHILE} ({type(e).__name__}). "
            "En Windows instala la base de zonas con: pip install tzdata"
        ) from None


def en_ventana(programada: datetime, ahora: datetime) -> bool:
    """True si la hora programada ya pasó y fue hace 6 horas o menos.

    Se resta en UTC: restar dos horas con la misma zona compara la hora de reloj y se
    equivoca en una hora cuando hay cambio de horario entre medio.
    """
    transcurrido = ahora.astimezone(timezone.utc) - programada.astimezone(timezone.utc)
    return timedelta(0) <= transcurrido <= VENTANA


def leer_ahora(texto: str, tz) -> datetime:
    """Interpreta --ahora. Sin zona horaria, es hora de Chile."""
    try:
        momento = datetime.fromisoformat(texto.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ErrorConfig(f"--ahora no se entiende: {texto!r}. Usa por ejemplo 2026-11-16T18:30") from None
    if momento.tzinfo is None:
        return momento.replace(tzinfo=tz)
    return momento.astimezone(tz)


def fmt_registro(momento: datetime) -> str:
    return momento.isoformat(timespec="minutes")


def fmt_corto(momento: datetime) -> str:
    return momento.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- Historias (CSV)


@dataclass(frozen=True)
class Historia:
    id: str
    fecha: date
    hora: time
    url: str
    url_jpg: str
    tanda: int
    origen: str
    programada: datetime


def url_jpeg(url: str) -> str:
    """La misma URL con .jpg (la API de Instagram solo acepta JPEG)."""
    partes = urllib.parse.urlsplit(url)
    base, punto, extension = partes.path.rpartition(".")
    extension = extension.lower()
    if punto and extension in ("jpg", "jpeg"):
        return url
    if punto and extension == "png":
        return urllib.parse.urlunsplit(partes._replace(path=base + ".jpg"))
    raise ErrorConfig(f"No sé cuál es la versión JPEG de {url}")


def _leer_texto(ruta: Path) -> str:
    try:
        return ruta.read_text(encoding="utf-8-sig")  # tolera BOM
    except UnicodeDecodeError:
        raise ErrorConfig(f"{ruta} no está en UTF-8") from None


def _leer_hora(texto: str, origen: str) -> time:
    texto = texto.strip()
    if not texto:
        return HORA_POR_DEFECTO
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", texto)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ErrorConfig(f"{origen}: hora inválida {texto!r} (usa HH:MM)")
    return time(int(m.group(1)), int(m.group(2)))


def leer_csv_tanda(ruta: Path, tanda: int, tz, nombre: str | None = None) -> list[Historia]:
    """Lee un historias-programables.csv (separador ';', BOM posible, CRLF o LF).

    Columnas: ID;fecha;tipo;archivo;URL y, si existe, una columna opcional 'hora'
    (HH:MM, hora de Chile). Solo se toman las filas de tipo Historia.
    """
    nombre = nombre or f"tanda-{tanda}/{ARCHIVO_CSV}"
    lector = csv.reader(io.StringIO(_leer_texto(ruta)), delimiter=";")
    filas = list(lector)
    if not filas:
        return []
    columnas = {c.strip().lower(): i for i, c in enumerate(filas[0])}
    faltan = [c for c in ("id", "fecha", "tipo", "url") if c not in columnas]
    if faltan:
        raise ErrorConfig(f"{nombre}: faltan las columnas {', '.join(faltan)}")

    def celda(fila: list[str], columna: str) -> str:
        i = columnas.get(columna)
        return fila[i].strip() if i is not None and i < len(fila) else ""

    historias = []
    for numero, fila in enumerate(filas[1:], start=2):
        if not any(c.strip() for c in fila):
            continue
        origen = f"{nombre} línea {numero}"
        if not celda(fila, "tipo").lower().startswith("historia"):
            continue
        id_ = celda(fila, "id")
        if not id_:
            raise ErrorConfig(f"{origen}: falta el ID")
        try:
            fecha = date.fromisoformat(celda(fila, "fecha"))
        except ValueError:
            raise ErrorConfig(f"{origen}: fecha inválida {celda(fila, 'fecha')!r} (usa AAAA-MM-DD)") from None
        hora = _leer_hora(celda(fila, "hora"), origen)
        url = celda(fila, "url")
        if not url.startswith("https://"):
            raise ErrorConfig(f"{origen}: la URL debe empezar con https:// ({url!r})")
        historias.append(
            Historia(
                id=id_,
                fecha=fecha,
                hora=hora,
                url=url,
                url_jpg=url_jpeg(url),
                tanda=tanda,
                origen=origen,
                programada=datetime.combine(fecha, hora, tzinfo=tz),
            )
        )
    return historias


def listar_tandas(repo: Path) -> list[tuple[int, Path]]:
    """Los tanda-N/historias-programables.csv del repositorio, de la más vieja a la más nueva."""
    tandas = []
    for carpeta in repo.iterdir():
        m = PATRON_TANDA.match(carpeta.name)
        if m and carpeta.is_dir() and (carpeta / ARCHIVO_CSV).is_file():
            tandas.append((int(m.group(1)), carpeta / ARCHIVO_CSV))
    return sorted(tandas)


def tanda_mas_nueva(historias: list[Historia]) -> list[Historia]:
    """Para cada fecha deja solo la fila de la tanda más nueva que tenga esa fecha.

    Así sale una sola Historia por día. Si la tanda ganadora trae dos filas para la
    misma fecha, o si dos fechas terminan con el mismo ID, es un error de configuración.
    """
    ganadora: dict[date, int] = {}
    for h in historias:
        ganadora[h.fecha] = max(ganadora.get(h.fecha, h.tanda), h.tanda)

    elegidas: dict[date, Historia] = {}
    for h in historias:
        if h.tanda != ganadora[h.fecha]:
            continue
        if h.fecha in elegidas:
            raise ErrorConfig(
                f"La fecha {h.fecha} aparece dos veces en tanda-{h.tanda} "
                f"({elegidas[h.fecha].origen} y {h.origen}). Debe haber una sola Historia por día."
            )
        elegidas[h.fecha] = h

    por_id: dict[str, Historia] = {}
    for h in elegidas.values():
        if h.id in por_id:
            raise ErrorConfig(f"El ID {h.id} se repite en {por_id[h.id].origen} y {h.origen}")
        por_id[h.id] = h
    return sorted(elegidas.values(), key=lambda h: h.programada)


def cargar_historias(repo: Path, desde: date, tz) -> list[Historia]:
    tandas = listar_tandas(repo)
    if not tandas:
        raise ErrorConfig(f"No encontré ningún tanda-N/{ARCHIVO_CSV} en {repo}")
    todas: list[Historia] = []
    for numero, ruta in tandas:
        todas.extend(leer_csv_tanda(ruta, numero, tz))
    return tanda_mas_nueva([h for h in todas if h.fecha >= desde])


def leer_desde(ruta: Path) -> date:
    if not ruta.is_file():
        raise ErrorConfig(f"Falta {ruta.name}: debe tener una fecha AAAA-MM-DD")
    for linea in _leer_texto(ruta).splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#"):
            try:
                return date.fromisoformat(linea)
            except ValueError:
                raise ErrorConfig(f"{ruta.name}: fecha inválida {linea!r} (usa AAAA-MM-DD)") from None
    raise ErrorConfig(f"{ruta.name} está vacío: debe tener una fecha AAAA-MM-DD")


# ---------------------------------------------------------------- registro


@dataclass
class EstadoRegistro:
    publicada: bool = False
    incierta: bool = False
    errores: int = 0
    otros: list[str] = field(default_factory=list)


def leer_registro(ruta: Path) -> list[dict[str, str]]:
    if not ruta.exists():
        return []
    lector = csv.DictReader(io.StringIO(_leer_texto(ruta)), delimiter=";")
    if not lector.fieldnames:
        return []
    columnas = [c.strip() for c in lector.fieldnames]
    if "ID" not in columnas or "estado" not in columnas:
        raise ErrorConfig(f"{ruta.name}: el encabezado debe ser {';'.join(COLUMNAS_REGISTRO)}")
    lector.fieldnames = columnas
    return [
        {k: (v or "").strip() for k, v in fila.items() if k is not None}
        for fila in lector
        if (fila.get("ID") or "").strip()
    ]


def resumir_registro(filas: list[dict[str, str]]) -> dict[str, EstadoRegistro]:
    resumen: dict[str, EstadoRegistro] = {}
    for fila in filas:
        est = resumen.setdefault(fila["ID"], EstadoRegistro())
        estado = fila.get("estado", "").lower()
        if estado == ESTADO_PUBLICADA:
            est.publicada = True
        elif estado == ESTADO_INCIERTO:
            est.incierta = True
        elif estado == ESTADO_ERROR:
            est.errores += 1
        else:
            est.otros.append(estado or "(vacío)")
    return resumen


def motivo_bloqueo(est: EstadoRegistro | None) -> str | None:
    """Por qué una Historia ya no se intenta, o None si se puede intentar."""
    if est is None:
        return None
    if est.publicada:
        return "ya publicada"
    if est.incierta:
        return "intento incierto: revisa Instagram (ver README)"
    if est.otros:
        return f"estado desconocido en el registro ({', '.join(est.otros)})"
    if est.errores >= MAX_INTENTOS:
        return f"{est.errores} intentos fallidos: no se reintenta"
    return None


def agregar_registro(ruta: Path, fila: dict[str, str]) -> None:
    nuevo = not ruta.exists() or ruta.stat().st_size == 0
    if not nuevo and ruta.read_bytes()[-1:] not in (b"\n", b"\r"):
        with ruta.open("ab") as f:
            f.write(b"\n")
    with ruta.open("a", encoding="utf-8", newline="") as f:
        escritor = csv.writer(f, delimiter=";", lineterminator="\n")
        if nuevo:
            escritor.writerow(COLUMNAS_REGISTRO)
        valores = []
        for columna in COLUMNAS_REGISTRO:
            valor = limpiar(fila.get(columna, ""))
            valor = " ".join(valor.split())  # sin saltos de línea
            valores.append(valor[:500])
        escritor.writerow(valores)


# ---------------------------------------------------------------- HTTP


def _abrir_por_defecto(peticion, timeout):
    return urllib.request.urlopen(peticion, timeout=timeout)


def _describir_error_meta(cuerpo: bytes) -> tuple[str, bool]:
    """Texto del error que devolvió Meta y si venía en el formato JSON de la Graph API."""
    try:
        error = json.loads(cuerpo.decode("utf-8"))["error"]
        partes = [str(error.get("error_user_msg") or error.get("message") or "sin mensaje")]
        for clave, nombre in (("code", "code"), ("error_subcode", "subcode"), ("fbtrace_id", "fbtrace_id")):
            if error.get(clave) not in (None, ""):
                partes.append(f"{nombre} {error[clave]}")
        return f"{partes[0]} ({', '.join(partes[1:])})" if len(partes) > 1 else partes[0], True
    except Exception:
        return cuerpo[:200].decode("utf-8", "replace").strip() or "sin cuerpo", False


class Graph:
    """Cliente mínimo de la Graph API. El token va solo en la cabecera Authorization."""

    def __init__(self, token: str, abrir=None):
        self._token = token
        self._abrir = abrir or _abrir_por_defecto

    def get(self, ruta: str, params: dict | None = None) -> dict:
        return self._pedir("GET", ruta, params)

    def post(self, ruta: str, params: dict) -> dict:
        return self._pedir("POST", ruta, params)

    def _pedir(self, metodo: str, ruta: str, params: dict | None) -> dict:
        ruta = ruta.strip("/")
        url = f"{GRAPH_URL}/{ruta}"
        cuerpo = None
        if params and metodo == "GET":
            url += "?" + urllib.parse.urlencode(params)
        elif params:
            cuerpo = urllib.parse.urlencode(params).encode("ascii")
        peticion = urllib.request.Request(url, data=cuerpo, method=metodo)
        peticion.add_header("Authorization", "Bearer " + self._token)
        if cuerpo is not None:
            peticion.add_header("Content-Type", "application/x-www-form-urlencoded")
        etiqueta = f"{metodo} /{ruta}"
        try:
            with self._abrir(peticion, timeout=TIMEOUT_HTTP_S) as respuesta:
                datos = respuesta.read()
        except urllib.error.HTTPError as e:
            try:
                detalle, es_de_meta = _describir_error_meta(e.read())
            except Exception:
                detalle, es_de_meta = "sin cuerpo", False
            raise ErrorGraph(
                limpiar(f"Meta respondió HTTP {e.code} en {etiqueta}: {detalle}"),
                rechazo=es_de_meta and 400 <= e.code < 500,
            ) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            razon = getattr(e, "reason", None) or type(e).__name__
            raise ErrorGraph(limpiar(f"Sin respuesta clara de Meta en {etiqueta}: {razon}"), rechazo=False) from None
        try:
            respuesta_json = json.loads(datos.decode("utf-8"))
        except ValueError:
            raise ErrorGraph(f"Meta devolvió algo que no es JSON en {etiqueta}", rechazo=False) from None
        if isinstance(respuesta_json, dict) and "error" in respuesta_json:
            detalle, _ = _describir_error_meta(datos)
            raise ErrorGraph(limpiar(f"Meta devolvió un error en {etiqueta}: {detalle}"), rechazo=True)
        if not isinstance(respuesta_json, dict):
            raise ErrorGraph(f"Respuesta inesperada de Meta en {etiqueta}", rechazo=False)
        return respuesta_json


def verificar_jpeg(url: str, abrir=None) -> tuple[bool, str]:
    """HEAD a la URL pública del JPEG (sin token: solo va a GitHub)."""
    abrir = abrir or _abrir_por_defecto
    peticion = urllib.request.Request(url, method="HEAD")
    try:
        with abrir(peticion, timeout=TIMEOUT_HTTP_S) as respuesta:
            estado = getattr(respuesta, "status", 200)
            tipo = respuesta.headers.get("Content-Type", "") or ""
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        return False, f"sin respuesta ({getattr(e, 'reason', None) or type(e).__name__})"
    if estado != 200:
        return False, f"HTTP {estado}"
    if "jpeg" not in tipo.lower():
        return False, f"no es JPEG (Content-Type: {tipo or 'vacío'})"
    return True, f"HTTP 200, {tipo}"


# ---------------------------------------------------------------- Instagram


def obtener_ig_id(graph: Graph) -> str:
    datos = graph.get(PAGINA_ID, {"fields": "instagram_business_account"})
    ig_id = (datos.get("instagram_business_account") or {}).get("id")
    if not ig_id:
        raise ErrorGraph(
            f"La página {PAGINA_ID} no devolvió instagram_business_account: revisa que la cuenta "
            "de Instagram esté vinculada a la página y que el token tenga los permisos.",
            rechazo=True,
        )
    return str(ig_id)


def esperar_contenedor(graph: Graph, contenedor_id: str, dormir) -> None:
    """Consulta status_code cada 30 s hasta FINISHED, con un máximo de 5 minutos."""
    consultas = ESPERA_MAXIMA_S // INTERVALO_ESTADO_S + 1
    for i in range(consultas):
        if i:
            dormir(INTERVALO_ESTADO_S)
        datos = graph.get(contenedor_id, {"fields": "status_code,status"})
        codigo = datos.get("status_code")
        log(f"    status_code a los {i * INTERVALO_ESTADO_S} s: {codigo}")
        if codigo == "FINISHED":
            return
        if codigo in ("ERROR", "EXPIRED"):
            raise ErrorPublicacion(f"El contenedor {contenedor_id} quedó en {codigo}: {datos.get('status', '')}".strip())
        if codigo == "PUBLISHED":
            raise ErrorPublicacion(f"El contenedor {contenedor_id} ya figura PUBLISHED antes de publicarlo", incierto=True)
    raise ErrorPublicacion(f"El contenedor {contenedor_id} no llegó a FINISHED en {ESPERA_MAXIMA_S // 60} minutos")


class Intento:
    """Lleva la cuenta de si ya se llamó a media_publish (para no publicar dos veces)."""

    def __init__(self):
        self.publicacion_enviada = False


def publicar_historia(graph: Graph, ig_id: str, h: Historia, abrir, dormir, intento: Intento) -> str:
    """Publica una Historia. Devuelve el media_id ('' si se confirmó por el contenedor)."""
    ok, detalle = verificar_jpeg(h.url_jpg, abrir)
    if not ok:
        raise ErrorPublicacion(f"El JPEG no está disponible ({detalle}): {h.url_jpg}")
    log(f"    Creando el contenedor STORIES con {h.url_jpg}")
    contenedor = graph.post(f"{ig_id}/media", {"media_type": "STORIES", "image_url": h.url_jpg})
    contenedor_id = str(contenedor.get("id") or "")
    if not contenedor_id:
        raise ErrorPublicacion("Meta no devolvió el id del contenedor")
    esperar_contenedor(graph, contenedor_id, dormir)

    log("    Publicando con media_publish")
    intento.publicacion_enviada = True
    try:
        respuesta = graph.post(f"{ig_id}/media_publish", {"creation_id": contenedor_id})
    except ErrorGraph as e:
        if e.rechazo:  # Meta la rechazó de forma explícita: no se publicó
            raise ErrorPublicacion(f"media_publish rechazado: {e}") from None
        return _confirmar_por_contenedor(graph, contenedor_id, str(e))
    media_id = str(respuesta.get("id") or "")
    if not media_id:
        return _confirmar_por_contenedor(graph, contenedor_id, "media_publish no devolvió id")
    return media_id


def _confirmar_por_contenedor(graph: Graph, contenedor_id: str, causa: str) -> str:
    """Tras una respuesta dudosa de media_publish, mira si el contenedor quedó publicado."""
    try:
        codigo = graph.get(contenedor_id, {"fields": "status_code"}).get("status_code")
    except ErrorGraph as e:
        codigo = f"desconocido ({e})"
    if codigo == "PUBLISHED":
        log(f"    media_publish no respondió claro ({causa}), pero el contenedor quedó PUBLISHED")
        return ""
    raise ErrorPublicacion(
        f"No se sabe si se publicó: {causa}; status_code del contenedor {contenedor_id}: {codigo}",
        incierto=True,
    )


# ---------------------------------------------------------------- corrida


def _imprimir_listado(historias, pendientes, bloqueadas, resumen, ahora, desde) -> None:
    inicio = ahora - VENTANA
    log(f"Historias desde {desde}: {len(historias)} (una por día, tanda más nueva).")
    log(f"Ventana: programadas entre {fmt_corto(inicio)} y {fmt_corto(ahora)} (hora de Chile).")
    if pendientes:
        log("Publicaría:")
        for h in pendientes:
            est = resumen.get(h.id)
            numero = (est.errores if est else 0) + 1
            log(f"  {h.id}  programada {fmt_corto(h.programada)}  tanda-{h.tanda}  intento {numero} de {MAX_INTENTOS}")
            log(f"    {h.url_jpg}")
    else:
        log("Publicaría: nada (no hay Historias pendientes en la ventana).")
    for h, motivo in bloqueadas:
        log(f"  No se intenta {h.id} (programada {fmt_corto(h.programada)}): {motivo}")
    proximas = [h for h in historias if h.programada > ahora][:3]
    if proximas:
        log("Próximas: " + "; ".join(f"{h.id} {fmt_corto(h.programada)} tanda-{h.tanda}" for h in proximas))


def _simular(pendientes, token, abrir, activo) -> int:
    fallas = 0
    if pendientes:
        log("Revisión de los JPEG:")
    for h in pendientes:
        ok, detalle = verificar_jpeg(h.url_jpg, abrir)
        log(f"  {h.id}: {'OK' if ok else 'FALLA'} ({detalle})")
        fallas += 0 if ok else 1
    if activo:
        log("programador/activo.txt existe: el flujo programado está activo.")
    else:
        log("programador/activo.txt no existe: el flujo programado está en pausa y no publicaría.")
    if not token:
        log(
            "ERROR: falta META_TOKEN, así que no pude validar el acceso a Meta ni el id de Instagram. "
            "En GitHub se guarda como secreto del repositorio (Settings > Secrets and variables > Actions > "
            "New repository secret, nombre META_TOKEN); en el PC, como variable de entorno META_TOKEN."
        )
        log("Simulación: no se creó ni se publicó nada y no se tocó el registro.")
        return 1
    graph = Graph(token, abrir)
    try:
        ig_id = obtener_ig_id(graph)
        log(f"Acceso a Meta OK. Página {PAGINA_ID} -> cuenta de Instagram {ig_id}")
        usuario = graph.get(ig_id, {"fields": "username"}).get("username", "?")
        log(f"Usuario de Instagram: @{usuario}")
        limite = graph.get(f"{ig_id}/content_publishing_limit", {"fields": "quota_usage,config"})
        datos = (limite.get("data") or [{}])[0]
        cuota = (datos.get("config") or {}).get("quota_total", "?")
        log(f"Publicaciones por API en las últimas 24 h: {datos.get('quota_usage', '?')} de {cuota}")
    except ErrorGraph as e:
        log(f"ERROR al validar el acceso a Meta: {e}")
        fallas += 1
    log("Simulación: no se creó ni se publicó nada y no se tocó el registro.")
    return 1 if fallas else 0


def _publicar(pendientes, resumen, token, abrir, reloj, dormir, ruta_registro, tz) -> int:
    fallas = 0
    graph = Graph(token, abrir) if token else None
    ig_id, error_ig = None, None
    if graph:
        try:
            ig_id = obtener_ig_id(graph)
            log(f"Cuenta de Instagram: {ig_id}")
        except ErrorGraph as e:
            error_ig = str(e)

    for h in pendientes:
        est = resumen.get(h.id)
        numero = (est.errores if est else 0) + 1
        log(f"Publicando {h.id} (intento {numero} de {MAX_INTENTOS})")
        fila = {"ID": h.id, "programada": fmt_registro(h.programada)}
        intento = Intento()
        try:
            if not token:
                raise ErrorPublicacion("Falta el secreto META_TOKEN (Settings > Secrets and variables > Actions)")
            if error_ig:
                raise ErrorPublicacion(error_ig)
            media_id = publicar_historia(graph, ig_id, h, abrir, dormir, intento)
            momento = reloj().astimezone(tz)
            atraso = round((momento.astimezone(timezone.utc) - h.programada.astimezone(timezone.utc)).total_seconds() / 60)
            fila.update(publicada=fmt_registro(momento), atraso_min=str(atraso), media_id=media_id, estado=ESTADO_PUBLICADA)
            if not media_id:
                fila["error"] = "media_publish no respondió claro; confirmada por status_code=PUBLISHED; media_id desconocido"
            log(f"  OK: {h.id} publicada (media_id {media_id or 'desconocido'}, {atraso} min de atraso)")
        except Exception as e:
            incierto = isinstance(e, ErrorPublicacion) and e.incierto
            if not isinstance(e, (ErrorPublicacion, ErrorGraph)):
                # Falla inesperada: si ya se llamó a media_publish, puede que sí se haya publicado.
                incierto = intento.publicacion_enviada
                log(limpiar(traceback.format_exc()))
                e = f"{type(e).__name__}: {e}"
            fallas += 1
            momento = reloj().astimezone(tz)
            fila.update(
                estado=ESTADO_INCIERTO if incierto else ESTADO_ERROR,
                error=f"intento {numero} a las {fmt_registro(momento)}: {e}",
            )
            log(f"  ERROR en {h.id}: {e}")
            if incierto:
                log("  No se reintenta solo, para no publicar dos veces. Revisa Instagram (ver README).")
            elif numero >= MAX_INTENTOS:
                log("  Era el último intento: no se vuelve a intentar.")
            else:
                log("  Se reintenta una sola vez en la próxima corrida, si sigue dentro de la ventana de 6 h.")
        agregar_registro(ruta_registro, fila)
    return 1 if fallas else 0


def _argumentos(argv):
    parser = argparse.ArgumentParser(description="Publica las Historias programadas de Apruébalo en Instagram.")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--simular", action="store_true", help="no crea ni publica nada: lista y valida")
    modo.add_argument("--programado", action="store_true", help="corrida del flujo programado: exige programador/activo.txt")
    parser.add_argument("--ahora", help="fija la hora (solo con --simular), p. ej. 2026-11-16T18:30 (hora de Chile)")
    args = parser.parse_args(argv)
    if args.ahora and not args.simular:
        parser.error("--ahora solo se puede usar con --simular")
    return args


def main(argv=None, entorno=None, abrir=None, reloj=None, dormir=None, repo=None) -> int:
    args = _argumentos(argv)
    entorno = os.environ if entorno is None else entorno
    reloj = reloj or (lambda: datetime.now(timezone.utc))
    dormir = dormir or _time.sleep
    repo = Path(repo) if repo else Path(__file__).resolve().parent.parent
    carpeta = repo / "programador"
    token = (entorno.get("META_TOKEN") or "").strip()
    registrar_secreto(token)

    try:
        modo = "SIMULACIÓN" if args.simular else ("PROGRAMADO" if args.programado else "MANUAL (real)")
        log(f"Programador de Historias de Apruébalo - modo {modo}")
        # La pausa va primero: sin activo.txt, el programado termina bien pase lo que pase.
        activo = (carpeta / "activo.txt").exists()
        if args.programado and not activo:
            log("programador/activo.txt no existe: el programador está en pausa y no publica nada.")
            return 0

        if token and not re.fullmatch(r"[\x21-\x7e]+", token):
            raise ErrorConfig("META_TOKEN tiene caracteres no válidos (espacios o saltos de línea en medio)")
        tz = zona_chile()
        ahora = leer_ahora(args.ahora, tz) if args.ahora else reloj().astimezone(tz)
        log(f"Ahora: {fmt_corto(ahora)} hora de Chile (UTC{ahora.strftime('%z')[:3]}). Graph API {GRAPH_VERSION}.")

        desde = leer_desde(carpeta / "desde.txt")
        historias = cargar_historias(repo, desde, tz)
        ruta_registro = carpeta / "registro.csv"
        resumen = resumir_registro(leer_registro(ruta_registro))

        pendientes, bloqueadas = [], []
        for h in historias:
            if en_ventana(h.programada, ahora):
                motivo = motivo_bloqueo(resumen.get(h.id))
                if motivo:
                    bloqueadas.append((h, motivo))
                else:
                    pendientes.append(h)
        _imprimir_listado(historias, pendientes, bloqueadas, resumen, ahora, desde)

        if args.simular:
            return _simular(pendientes, token, abrir, activo)
        if not pendientes:
            log("Nada que publicar en esta corrida.")
            return 0
        return _publicar(pendientes, resumen, token, abrir, reloj, dormir, ruta_registro, tz)
    except ErrorConfig as e:
        log(f"ERROR de configuración: {e}")
        return 1
    except Exception:
        log("ERROR inesperado:")
        log(limpiar(traceback.format_exc()))
        return 1


if __name__ == "__main__":
    try:  # UTF-8 como en GitHub Actions, también en Windows con la salida redirigida
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())

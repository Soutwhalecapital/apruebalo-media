"""Pruebas del programador. Correr desde la raíz del repositorio con:

    python -m unittest discover -s programador -v

No usan la red ni el token real: Meta y GitHub se reemplazan por respuestas falsas.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publicar as p  # noqa: E402

TZ = p.zona_chile()
UTC = timezone.utc
REPO_REAL = Path(__file__).resolve().parent.parent
TOKEN_FALSO = "token-FALSO-de-prueba-1234567890"
IG_ID = "17841400000000000"
URL_BASE = "https://raw.githubusercontent.com/Soutwhalecapital/apruebalo-media/main"
ENCABEZADO = "ID;fecha;tipo;archivo;URL"


def chile(*args):
    return datetime(*args, tzinfo=TZ)


def fila(fecha, tanda, nombre=None):
    nombre = nombre or f"H-{fecha}"
    return f"H-{fecha};{fecha};Historia;historias/{nombre}.png;{URL_BASE}/tanda-{tanda}/historias/{nombre}.png"


def escribir_csv(repo, tanda, filas, encabezado=ENCABEZADO, bom=False, crlf=False):
    carpeta = Path(repo) / f"tanda-{tanda}"
    carpeta.mkdir(parents=True, exist_ok=True)
    salto = "\r\n" if crlf else "\n"
    texto = salto.join([encabezado] + filas) + salto
    (carpeta / p.ARCHIVO_CSV).write_bytes((b"\xef\xbb\xbf" if bom else b"") + texto.encode("utf-8"))


class Respuesta:
    def __init__(self, cuerpo=b"", estado=200, cabeceras=None):
        self._cuerpo = cuerpo
        self.status = estado
        self.headers = cabeceras or {}

    def read(self):
        return self._cuerpo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def error_http(url, codigo, cuerpo):
    return urllib.error.HTTPError(url, codigo, "error", {}, io.BytesIO(json.dumps(cuerpo).encode()))


class MetaFalsa:
    """Reemplaza urlopen. Anota cada petición y responde como Graph API y GitHub."""

    def __init__(self, estados=("FINISHED",), publicar="ok", jpeg_ok=True, estado_tras_publicar="PUBLISHED"):
        self.estados = list(estados)
        self.publicar = publicar
        self.jpeg_ok = jpeg_ok
        self.estado_tras_publicar = estado_tras_publicar
        self.peticiones = []
        self.publicado = False

    def __call__(self, peticion, timeout=None):
        url = peticion.full_url
        metodo = peticion.get_method()
        cuerpo = urllib.parse.parse_qs((peticion.data or b"").decode())
        self.peticiones.append((metodo, url, dict(peticion.header_items()), cuerpo))
        if url.startswith("https://raw.githubusercontent.com/"):
            if not self.jpeg_ok:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
            return Respuesta(estado=200, cabeceras={"Content-Type": "image/jpeg"})
        assert url.startswith(p.GRAPH_URL + "/"), url
        ruta = urllib.parse.urlsplit(url).path.split(p.GRAPH_VERSION + "/", 1)[1]
        if metodo == "GET" and ruta == p.PAGINA_ID:
            return self._json({"instagram_business_account": {"id": IG_ID}, "id": p.PAGINA_ID})
        if metodo == "POST" and ruta == f"{IG_ID}/media":
            return self._json({"id": "CONT1"})
        if metodo == "GET" and ruta == "CONT1":
            if self.publicado:
                return self._json({"status_code": self.estado_tras_publicar})
            estado = self.estados.pop(0) if len(self.estados) > 1 else self.estados[0]
            return self._json({"status_code": estado, "status": f"{estado}: detalle"})
        if metodo == "POST" and ruta == f"{IG_ID}/media_publish":
            if self.publicar == "ok":
                self.publicado = True
                return self._json({"id": "MEDIA99"})
            if self.publicar == "rechazo":
                raise error_http(url, 400, {"error": {"message": "(#9007) Media no disponible", "code": 9007}})
            if self.publicar == "sin_respuesta":
                self.publicado = True  # Meta sí la publicó, pero la respuesta se perdió
                raise TimeoutError("timed out")
            if self.publicar == "sin_respuesta_no_publicada":
                raise TimeoutError("timed out")
        if metodo == "GET" and ruta == IG_ID:
            return self._json({"username": "apruebalo.cl", "id": IG_ID})
        if metodo == "GET" and ruta == f"{IG_ID}/content_publishing_limit":
            return self._json({"data": [{"quota_usage": 1, "config": {"quota_total": 100}}]})
        raise AssertionError(f"Petición no esperada: {metodo} {url}")

    @staticmethod
    def _json(datos):
        return Respuesta(json.dumps(datos).encode())

    def graph(self):
        return [x for x in self.peticiones if x[1].startswith(p.GRAPH_URL)]

    def rutas(self):
        return [(m, urllib.parse.urlsplit(u).path.rsplit("/", 2)[-2:]) for m, u, _, _ in self.peticiones]


class RepoFalso:
    """Repositorio temporal con una Historia el 2026-11-16 (tanda-3) y otra el 17 (tanda-3)."""

    def __init__(self, activo=False, registro=None):
        self.dir = tempfile.TemporaryDirectory()
        self.raiz = Path(self.dir.name)
        (self.raiz / "programador").mkdir()
        (self.raiz / "programador" / "desde.txt").write_text("2026-11-16\n", encoding="utf-8")
        cabecera = ";".join(p.COLUMNAS_REGISTRO) + "\n"
        (self.raiz / "programador" / "registro.csv").write_text(cabecera + (registro or ""), encoding="utf-8")
        if activo:
            (self.raiz / "programador" / "activo.txt").write_text("", encoding="utf-8")
        escribir_csv(self.raiz, 1, [fila("2026-11-12", 1), fila("2026-11-17", 1)], crlf=True)
        escribir_csv(self.raiz, 3, [fila("2026-11-16", 3), fila("2026-11-17", 3)])

    @property
    def registro(self):
        return (self.raiz / "programador" / "registro.csv").read_text(encoding="utf-8")

    def correr(self, *argv, token=TOKEN_FALSO, meta=None, ahora=chile(2026, 11, 16, 18, 7)):
        meta = meta or MetaFalsa()
        esperas = []
        salida = io.StringIO()
        entorno = {"META_TOKEN": token} if token else {}
        with contextlib.redirect_stdout(salida):
            codigo = p.main(
                list(argv),
                entorno=entorno,
                abrir=meta,
                reloj=lambda: ahora.astimezone(UTC),
                dormir=esperas.append,
                repo=self.raiz,
            )
        return codigo, salida.getvalue(), meta, esperas

    def cerrar(self):
        self.dir.cleanup()


# ------------------------------------------------------------------ fechas y ventana


class PruebasVentana(unittest.TestCase):
    def test_ventana_de_6_horas(self):
        prog = chile(2026, 11, 16, 18, 0)
        self.assertFalse(p.en_ventana(prog, chile(2026, 11, 16, 17, 59)))  # todavía no
        self.assertTrue(p.en_ventana(prog, chile(2026, 11, 16, 18, 0)))
        self.assertTrue(p.en_ventana(prog, chile(2026, 11, 16, 18, 7)))
        self.assertTrue(p.en_ventana(prog, chile(2026, 11, 16, 23, 59)))
        self.assertTrue(p.en_ventana(prog, chile(2026, 11, 17, 0, 0)))  # justo 6 h
        self.assertFalse(p.en_ventana(prog, chile(2026, 11, 17, 0, 1)))

    def test_verano_e_invierno_en_chile(self):
        self.assertEqual(chile(2026, 11, 16, 18, 0).utcoffset(), timedelta(hours=-3))
        self.assertEqual(chile(2026, 7, 15, 18, 0).utcoffset(), timedelta(hours=-4))
        # La misma hora UTC es 18:30 en noviembre (UTC-3) y 17:30 en julio (UTC-4).
        self.assertTrue(p.en_ventana(chile(2026, 11, 16, 18, 0), datetime(2026, 11, 16, 21, 30, tzinfo=UTC)))
        self.assertFalse(p.en_ventana(chile(2026, 7, 15, 18, 0), datetime(2026, 7, 15, 21, 30, tzinfo=UTC)))

    def test_cambio_de_hora_abril_se_mide_en_tiempo_real(self):
        # El 4-4-2027 a las 00:00 (UTC-3) el reloj vuelve a las 23:00 del 3-4 (UTC-4).
        prog = chile(2027, 4, 3, 18, 0)
        self.assertEqual(prog.utcoffset(), timedelta(hours=-3))
        ahora = datetime(2027, 4, 4, 3, 30, tzinfo=UTC)
        local = ahora.astimezone(TZ)
        self.assertEqual((local.day, local.hour, local.minute), (3, 23, 30))
        self.assertEqual(local.utcoffset(), timedelta(hours=-4))
        # En el reloj pasaron 5 h 30, pero en tiempo real 6 h 30: queda fuera.
        self.assertFalse(p.en_ventana(prog, local))
        self.assertTrue(p.en_ventana(prog, datetime(2027, 4, 4, 2, 30, tzinfo=UTC).astimezone(TZ)))

    def test_cambio_de_hora_septiembre(self):
        # El 6-9-2026 a las 00:00 (UTC-4) el reloj salta a la 01:00 (UTC-3).
        prog = chile(2026, 9, 5, 18, 0)
        self.assertEqual(prog.utcoffset(), timedelta(hours=-4))
        self.assertEqual(chile(2026, 9, 6, 18, 0).utcoffset(), timedelta(hours=-3))
        self.assertTrue(p.en_ventana(prog, datetime(2026, 9, 6, 3, 59, tzinfo=UTC).astimezone(TZ)))
        self.assertFalse(p.en_ventana(prog, datetime(2026, 9, 6, 4, 30, tzinfo=UTC).astimezone(TZ)))

    def test_ahora_sin_zona_es_hora_de_chile(self):
        ahora = p.leer_ahora("2026-11-16T18:30", TZ)
        self.assertEqual(ahora.astimezone(UTC), datetime(2026, 11, 16, 21, 30, tzinfo=UTC))
        self.assertEqual(p.leer_ahora("2026-11-16T21:30Z", TZ), ahora)
        with self.assertRaises(p.ErrorConfig):
            p.leer_ahora("mañana", TZ)

    def test_desde_y_hora_por_defecto_y_columna_hora(self):
        with tempfile.TemporaryDirectory() as d:
            escribir_csv(
                d, 1,
                ["H-1;2026-11-15;Historia;a.png;https://x/a.png;10:30",
                 "H-2;2026-11-16;Historia;b.png;https://x/b.png;10:30",
                 "H-3;2026-11-17;Historia;c.png;https://x/c.png;",
                 "P-4;2026-11-18;Post;d.png;https://x/d.png;"],
                encabezado="ID;fecha;tipo;archivo;URL;hora",
            )
            historias = p.cargar_historias(Path(d), date(2026, 11, 16), TZ)
        self.assertEqual([h.id for h in historias], ["H-2", "H-3"])  # sin el 15 ni el Post
        self.assertEqual(historias[0].programada, chile(2026, 11, 16, 10, 30))
        self.assertEqual(historias[1].programada, chile(2026, 11, 17, 18, 0))
        self.assertEqual(historias[0].url_jpg, "https://x/b.jpg")

    def test_sin_columna_hora_son_las_18(self):
        with tempfile.TemporaryDirectory() as d:
            escribir_csv(d, 1, [fila("2026-11-16", 1)])
            (h,) = p.cargar_historias(Path(d), date(2026, 11, 16), TZ)
        self.assertEqual(h.hora, time(18, 0))

    def test_fecha_invalida_es_error_claro(self):
        with tempfile.TemporaryDirectory() as d:
            escribir_csv(d, 1, ["H-x;16-11-2026;Historia;a.png;https://x/a.png"])
            with self.assertRaisesRegex(p.ErrorConfig, "fecha inválida"):
                p.cargar_historias(Path(d), date(2026, 11, 16), TZ)

    def test_url_jpeg(self):
        self.assertEqual(p.url_jpeg("https://x/h/H-1.png"), "https://x/h/H-1.jpg")
        self.assertEqual(p.url_jpeg("https://x/h/H-1.PNG"), "https://x/h/H-1.jpg")
        self.assertEqual(p.url_jpeg("https://x/h/H-1.jpg"), "https://x/h/H-1.jpg")
        with self.assertRaises(p.ErrorConfig):
            p.url_jpeg("https://x/h/H-1.webp")


# ------------------------------------------------------------------ tanda más nueva


class PruebasTandaMasNueva(unittest.TestCase):
    def test_gana_la_tanda_mas_nueva_por_fecha(self):
        with tempfile.TemporaryDirectory() as d:
            escribir_csv(d, 1, [fila("2026-11-17", 1), fila("2026-11-20", 1)], crlf=True)
            escribir_csv(d, 2, [fila("2026-12-02", 2)], crlf=True)
            escribir_csv(d, 3, [fila("2026-11-17", 3), fila("2026-12-02", 3)], bom=True)
            historias = p.cargar_historias(Path(d), date(2026, 11, 16), TZ)
        por_fecha = {h.fecha.isoformat(): h for h in historias}
        self.assertEqual(len(historias), 3)  # una por día
        self.assertEqual(por_fecha["2026-11-17"].tanda, 3)
        self.assertIn("/tanda-3/", por_fecha["2026-11-17"].url_jpg)
        self.assertEqual(por_fecha["2026-12-02"].tanda, 3)
        self.assertEqual(por_fecha["2026-11-20"].tanda, 1)  # solo está en tanda-1

    def test_orden_numerico_de_tandas(self):
        with tempfile.TemporaryDirectory() as d:
            escribir_csv(d, 9, [fila("2026-11-17", 9)])
            escribir_csv(d, 10, [fila("2026-11-17", 10)])
            (h,) = p.cargar_historias(Path(d), date(2026, 11, 16), TZ)
        self.assertEqual(h.tanda, 10)

    def test_fecha_repetida_en_la_tanda_ganadora_es_error(self):
        with tempfile.TemporaryDirectory() as d:
            escribir_csv(d, 3, [fila("2026-11-17", 3), fila("2026-11-17", 3, "otra")])
            with self.assertRaisesRegex(p.ErrorConfig, "aparece dos veces"):
                p.cargar_historias(Path(d), date(2026, 11, 16), TZ)

    def test_fecha_repetida_en_tanda_vieja_no_importa(self):
        with tempfile.TemporaryDirectory() as d:
            escribir_csv(d, 1, [fila("2026-11-17", 1), fila("2026-11-17", 1, "otra")])
            escribir_csv(d, 3, [fila("2026-11-17", 3)])
            (h,) = p.cargar_historias(Path(d), date(2026, 11, 16), TZ)
        self.assertEqual(h.tanda, 3)

    def test_csv_reales_del_repositorio(self):
        historias = p.cargar_historias(REPO_REAL, date(2026, 11, 16), TZ)
        fechas = [h.fecha for h in historias]
        self.assertEqual(len(fechas), len(set(fechas)), "más de una Historia el mismo día")
        por_fecha = {h.fecha.isoformat(): h for h in historias}
        # Pisadas conocidas: tanda-1 y tanda-3 el 17-11; tanda-2 y tanda-3 el 02-12. Manda tanda-3.
        self.assertEqual(por_fecha["2026-11-17"].tanda, 3)
        self.assertTrue(por_fecha["2026-11-17"].url_jpg.endswith("/tanda-3/historias/H-2026-11-17.jpg"))
        self.assertEqual(por_fecha["2026-12-02"].tanda, 3)
        self.assertTrue(por_fecha["2026-12-02"].url_jpg.endswith("/tanda-3/historias/H-2026-12-02.jpg"))
        # Entre el 16-11 y el 13-12 todo sale de tanda-3; después, de tanda-2.
        for h in historias:
            self.assertEqual(h.tanda, 3 if h.fecha <= date(2026, 12, 13) else 2, h.id)
        self.assertEqual(historias[0].id, "H-2026-11-16")
        self.assertEqual(len(historias), 34)
        # Cada JPEG existe en el repositorio con la misma ruta que la URL.
        for h in historias:
            ruta = urllib.parse.urlsplit(h.url_jpg).path.split("/main/", 1)[1]
            self.assertTrue((REPO_REAL / ruta).is_file(), ruta)


# ------------------------------------------------------------------ registro


class PruebasRegistro(unittest.TestCase):
    def test_escribe_y_lee(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "registro.csv"
            p.agregar_registro(ruta, {"ID": "H-1", "programada": "2026-11-16T18:00-03:00",
                                      "publicada": "2026-11-16T18:07-03:00", "atraso_min": "7",
                                      "media_id": "123", "estado": "publicada"})
            p.agregar_registro(ruta, {"ID": "H-2", "programada": "2026-11-17T18:00-03:00",
                                      "estado": "error", "error": "falla; con punto y coma\ny salto"})
            texto = ruta.read_text(encoding="utf-8")
            filas = p.leer_registro(ruta)
        self.assertTrue(texto.startswith("ID;programada;publicada;atraso_min;media_id;estado;error\n"))
        self.assertEqual(len(texto.splitlines()), 3)
        self.assertEqual(filas[0]["media_id"], "123")
        self.assertEqual(filas[0]["atraso_min"], "7")
        self.assertEqual(filas[1]["error"], "falla; con punto y coma y salto")
        self.assertEqual(filas[1]["publicada"], "")

    def test_lee_con_bom_y_crlf_y_sin_salto_final(self):
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "registro.csv"
            ruta.write_bytes(b"\xef\xbb\xbfID;programada;publicada;atraso_min;media_id;estado;error\r\n"
                             b"H-1;a;b;7;9;publicada;")
            self.assertEqual(p.leer_registro(ruta)[0]["estado"], "publicada")
            p.agregar_registro(ruta, {"ID": "H-2", "estado": "error"})
            self.assertEqual([f["ID"] for f in p.leer_registro(ruta)], ["H-1", "H-2"])

    def test_registro_vacio_o_inexistente(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(p.leer_registro(Path(d) / "no.csv"), [])
        self.assertEqual(p.leer_registro(REPO_REAL / "programador" / "registro.csv"), [])

    def test_reglas_de_reintento(self):
        def motivo(*estados):
            filas = [{"ID": "H-1", "estado": e} for e in estados]
            return p.motivo_bloqueo(p.resumir_registro(filas).get("H-1"))

        self.assertIsNone(motivo())                       # nunca intentada
        self.assertIsNone(motivo("error"))                # 1 error: se reintenta una vez
        self.assertIn("2 intentos", motivo("error", "error"))
        self.assertEqual(motivo("publicada"), "ya publicada")
        self.assertEqual(motivo("error", "publicada"), "ya publicada")
        self.assertIn("incierto", motivo("incierto"))     # nunca se reintenta solo
        self.assertIn("desconocido", motivo("otra cosa"))  # ante la duda, no se publica

    def test_el_token_no_se_guarda(self):
        p.registrar_secreto(TOKEN_FALSO)
        with tempfile.TemporaryDirectory() as d:
            ruta = Path(d) / "registro.csv"
            p.agregar_registro(ruta, {"ID": "H-1", "estado": "error", "error": f"x access_token={TOKEN_FALSO} y"})
            texto = ruta.read_text(encoding="utf-8")
        self.assertNotIn(TOKEN_FALSO, texto)
        self.assertIn("access_token=*** y", texto)


# ------------------------------------------------------------------ corridas completas


class PruebasCorrida(unittest.TestCase):
    def setUp(self):
        self.repo = None

    def tearDown(self):
        if self.repo:
            self.repo.cerrar()

    def nuevo(self, **kw):
        self.repo = RepoFalso(**kw)
        return self.repo

    def test_programado_sin_activo_no_publica_y_termina_bien(self):
        repo = self.nuevo(activo=False)
        antes = repo.registro
        codigo, salida, meta, _ = repo.correr("--programado")
        self.assertEqual(codigo, 0)
        self.assertEqual(meta.peticiones, [])  # ni Meta ni GitHub
        self.assertEqual(repo.registro, antes)
        self.assertIn("activo.txt no existe", salida)

    def test_programado_sin_activo_termina_bien_aunque_falte_el_token(self):
        repo = self.nuevo(activo=False)
        codigo, _, meta, _ = repo.correr("--programado", token=None)
        self.assertEqual((codigo, meta.peticiones), (0, []))

    def test_simular_sin_token_lista_y_falla_limpio(self):
        repo = self.nuevo(activo=False)
        antes = repo.registro
        codigo, salida, meta, _ = repo.correr("--simular", "--ahora", "2026-11-16T18:30", token=None)
        self.assertEqual(codigo, 1)
        self.assertIn("H-2026-11-16", salida)
        self.assertIn("tanda-3/historias/H-2026-11-16.jpg", salida)
        self.assertIn("falta META_TOKEN", salida)
        self.assertNotIn("Traceback", salida)
        self.assertEqual(meta.graph(), [])  # no habló con Meta
        self.assertEqual(repo.registro, antes)

    def test_simular_con_token_valida_sin_crear_ni_publicar(self):
        repo = self.nuevo(activo=False)
        codigo, salida, meta, _ = repo.correr("--simular", "--ahora", "2026-11-16T18:30")
        self.assertEqual(codigo, 0, salida)
        metodos = {m for m, _, _, _ in meta.graph()}
        self.assertEqual(metodos, {"GET"})  # nada de POST
        self.assertIn(IG_ID, salida)
        self.assertNotIn(TOKEN_FALSO, salida)
        self.assertEqual(repo.registro, ";".join(p.COLUMNAS_REGISTRO) + "\n")

    def test_ahora_solo_con_simular(self):
        repo = self.nuevo()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            repo.correr("--ahora", "2026-11-16T18:30")
        self.assertEqual(cm.exception.code, 2)

    def test_publica_y_anota(self):
        repo = self.nuevo(activo=True)
        meta = MetaFalsa(estados=("IN_PROGRESS", "FINISHED"))
        codigo, salida, meta, esperas = repo.correr("--programado", meta=meta)
        self.assertEqual(codigo, 0, salida)
        self.assertEqual(esperas, [30])  # una espera de 30 s entre consultas
        crear = [x for x in meta.peticiones if x[0] == "POST" and x[1].endswith("/media")][0]
        self.assertEqual(crear[3], {"media_type": ["STORIES"], "image_url": [f"{URL_BASE}/tanda-3/historias/H-2026-11-16.jpg"]})
        publicar = [x for x in meta.peticiones if x[1].endswith("/media_publish")][0]
        self.assertEqual(publicar[3], {"creation_id": ["CONT1"]})
        (f,) = p.leer_registro(repo.raiz / "programador" / "registro.csv")
        self.assertEqual((f["ID"], f["estado"], f["media_id"], f["atraso_min"]), ("H-2026-11-16", "publicada", "MEDIA99", "7"))
        self.assertEqual((f["programada"], f["publicada"]), ("2026-11-16T18:00-03:00", "2026-11-16T18:07-03:00"))
        # El token va solo en la cabecera, y solo a Meta.
        for metodo, url, cabeceras, cuerpo in meta.peticiones:
            self.assertNotIn(TOKEN_FALSO, url)
            self.assertNotIn(TOKEN_FALSO, json.dumps(cuerpo))
            if url.startswith(p.GRAPH_URL):
                self.assertEqual(cabeceras.get("Authorization"), "Bearer " + TOKEN_FALSO)
            else:
                self.assertNotIn("Authorization", cabeceras)
        self.assertNotIn(TOKEN_FALSO, salida)
        # Una segunda corrida no la vuelve a publicar.
        codigo2, salida2, meta2, _ = repo.correr("--programado", ahora=chile(2026, 11, 16, 18, 37))
        self.assertEqual((codigo2, meta2.peticiones), (0, []))
        self.assertIn("ya publicada", salida2)

    def test_manual_real_no_exige_activo(self):
        repo = self.nuevo(activo=False)
        codigo, salida, _, _ = repo.correr()
        self.assertEqual(codigo, 0, salida)
        self.assertEqual(p.leer_registro(repo.raiz / "programador" / "registro.csv")[0]["estado"], "publicada")

    def test_sin_token_anota_error_y_falla(self):
        repo = self.nuevo(activo=True)
        codigo, salida, meta, _ = repo.correr("--programado", token=None)
        self.assertEqual(codigo, 1)
        self.assertEqual(meta.peticiones, [])
        (f,) = p.leer_registro(repo.raiz / "programador" / "registro.csv")
        self.assertEqual(f["estado"], "error")
        self.assertIn("META_TOKEN", f["error"])

    def test_un_reintento_y_despues_para(self):
        repo = self.nuevo(activo=True)
        fallo = MetaFalsa(estados=("ERROR",))
        self.assertEqual(repo.correr("--programado", meta=fallo)[0], 1)  # 18:07 falla: correo 1
        codigo, salida, _, _ = repo.correr("--programado", meta=MetaFalsa(estados=("ERROR",)),
                                           ahora=chile(2026, 11, 16, 18, 37))
        self.assertEqual(codigo, 1)  # 18:37 reintenta y falla: correo 2
        self.assertIn("intento 2 de 2", salida)
        self.assertIn("último intento", salida)
        codigo, salida, meta, _ = repo.correr("--programado", ahora=chile(2026, 11, 16, 19, 7))
        self.assertEqual((codigo, meta.peticiones), (0, []))  # 19:07 ya no insiste: sin correo
        self.assertIn("2 intentos fallidos", salida)
        estados = [f["estado"] for f in p.leer_registro(repo.raiz / "programador" / "registro.csv")]
        self.assertEqual(estados, ["error", "error"])

    def test_reintento_que_sale_bien(self):
        repo = self.nuevo(activo=True, registro="H-2026-11-16;2026-11-16T18:00-03:00;;;;error;intento 1: red\n")
        codigo, salida, _, _ = repo.correr("--programado", ahora=chile(2026, 11, 16, 18, 37))
        self.assertEqual(codigo, 0, salida)
        estados = [f["estado"] for f in p.leer_registro(repo.raiz / "programador" / "registro.csv")]
        self.assertEqual(estados, ["error", "publicada"])

    def test_contenedor_que_no_termina_en_5_minutos(self):
        repo = self.nuevo(activo=True)
        codigo, _, meta, esperas = repo.correr("--programado", meta=MetaFalsa(estados=("IN_PROGRESS",)))
        self.assertEqual(codigo, 1)
        self.assertEqual(esperas, [30] * 10)  # 11 consultas: 0, 30, ..., 300 s
        self.assertFalse(any(u.endswith("/media_publish") for _, u, _, _ in meta.peticiones))
        (f,) = p.leer_registro(repo.raiz / "programador" / "registro.csv")
        self.assertIn("5 minutos", f["error"])

    def test_jpeg_que_no_responde_no_crea_contenedor(self):
        repo = self.nuevo(activo=True)
        codigo, _, meta, _ = repo.correr("--programado", meta=MetaFalsa(jpeg_ok=False))
        self.assertEqual(codigo, 1)
        self.assertFalse(any(m == "POST" for m, _, _, _ in meta.peticiones))

    def test_media_publish_rechazado_es_error_reintentable(self):
        repo = self.nuevo(activo=True)
        codigo, _, _, _ = repo.correr("--programado", meta=MetaFalsa(publicar="rechazo"))
        self.assertEqual(codigo, 1)
        (f,) = p.leer_registro(repo.raiz / "programador" / "registro.csv")
        self.assertEqual(f["estado"], "error")
        self.assertIn("9007", f["error"])

    def test_media_publish_sin_respuesta_pero_publicada(self):
        repo = self.nuevo(activo=True)
        codigo, salida, _, _ = repo.correr("--programado", meta=MetaFalsa(publicar="sin_respuesta"))
        self.assertEqual(codigo, 0, salida)
        (f,) = p.leer_registro(repo.raiz / "programador" / "registro.csv")
        self.assertEqual((f["estado"], f["media_id"]), ("publicada", ""))

    def test_media_publish_sin_respuesta_queda_incierto_y_no_se_repite(self):
        repo = self.nuevo(activo=True)
        meta = MetaFalsa(publicar="sin_respuesta_no_publicada", estado_tras_publicar="FINISHED")
        codigo, salida, _, _ = repo.correr("--programado", meta=meta)
        self.assertEqual(codigo, 1)
        self.assertIn("para no publicar dos veces", salida)
        (f,) = p.leer_registro(repo.raiz / "programador" / "registro.csv")
        self.assertEqual(f["estado"], "incierto")
        codigo, salida, meta, _ = repo.correr("--programado", ahora=chile(2026, 11, 16, 18, 37))
        self.assertEqual((codigo, meta.peticiones), (0, []))

    def test_el_token_no_aparece_aunque_meta_lo_repita(self):
        repo = self.nuevo(activo=True)

        class MetaQueRepite(MetaFalsa):
            def __call__(self, peticion, timeout=None):
                if peticion.full_url.startswith(p.GRAPH_URL):
                    raise error_http(peticion.full_url, 400, {"error": {
                        "message": f"Invalid OAuth access token {TOKEN_FALSO}", "code": 190}})
                return super().__call__(peticion, timeout)

        codigo, salida, _, _ = repo.correr("--programado", meta=MetaQueRepite())
        self.assertEqual(codigo, 1)
        self.assertNotIn(TOKEN_FALSO, salida)
        self.assertNotIn(TOKEN_FALSO, repo.registro)
        self.assertIn("Invalid OAuth access token ***", repo.registro)

    def test_csv_roto_falla_con_mensaje_claro(self):
        repo = self.nuevo(activo=True)
        escribir_csv(repo.raiz, 3, ["H-x;2026-13-40;Historia;a.png;https://x/a.png"])
        codigo, salida, _, _ = repo.correr("--programado")
        self.assertEqual(codigo, 1)
        self.assertIn("ERROR de configuración", salida)
        self.assertNotIn("Traceback", salida)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Bot de empleo público de Extremadura.

Lee el sumario del DOE (RSS oficial) y el del BOE (API de datos abiertos),
se queda con lo relativo a empleo público en Extremadura, lo clasifica y
lo envía a Telegram. No usa IA ni librerías externas.
"""
import html
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
AVISAR_SI_VACIO = os.environ.get("AVISAR_SI_VACIO", "") == "1"

ESTADO = Path("estado.json")
MAX_VISTOS = 5000
UA = "Mozilla/5.0 (compatible; bot-extremadura/1.0)"

DOE_RSS = "https://doe.juntaex.es/rss/rss.php?seccion=6"
BOE_API = "https://www.boe.es/datosabiertos/api/boe/sumario/{fecha}"

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


# ---------------------------------------------------------------- utilidades

def norm(texto):
    """Minúsculas y sin tildes, para comparar sin sorpresas."""
    t = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def descargar(url, accept=None):
    cabeceras = {"User-Agent": UA}
    if accept:
        cabeceras["Accept"] = accept
    req = urllib.request.Request(url, headers=cabeceras)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read()


# Todo se compara contra texto normalizado (sin tildes).
RE_EXTREMADURA = re.compile(r"extremadura|extremen|badajoz|caceres")

RE_EMPLEO = re.compile(
    r"procesos? selectivos?|pruebas? selectivas?|oferta de empleo publico"
    r"|personal (funcionario|laboral|estatutario|docente)|funcionari"
    r"|bolsas? de (trabajo|empleo)|listas? de espera"
    r"|concurso[- ]oposicion|oposicion(es)?\b|concurso de (meritos|traslados?)"
    r"|proveer|provision de (una|dos|tres|cuatro|varias|\d+|las?|puestos?|plazas?)"
    r"|plazas? vacantes?|estabilizacion|aspirantes|libre designacion"
)
# En la sección II del DOE (Autoridades y personal) basta con algo más laxo.
RE_EMPLEO_SEC2 = re.compile(
    r"concurso|puestos? de trabajo|tribunal|admitid|plazas?|integracion"
    r"|carrera profesional|seleccion"
)

RE_SEGUIMIENTO = re.compile(
    r"admitid|excluid|tribunal|aprobad|superad|nombra|adjudica"
    r"|toma de posesion|calificacion|puntuacion"
    r"|(relacion|lista|listado)s? (provisional|definitiv)"
    r"|fechas? (de|y|,)|lugar|ejercicio de la fase|realizacion del"
    r"|resuelve|declara desiert|cese|emplaza|recurso|sentencia|jubilacion"
)
RE_BOLSA = re.compile(r"bolsas? de (trabajo|empleo)|listas? de espera")
RE_OFERTA = re.compile(r"oferta de empleo publico")
RE_CONVOCATORIA = re.compile(
    r"convoca|bases|pruebas? selectivas?|proveer|provision|libre designacion"
    r"|concurso"
)

GRUPOS = [
    ("convocatorias", "🆕 <b>Convocatorias nuevas</b>", 230),
    ("ofertas", "📊 <b>Ofertas de empleo público</b>", 230),
    ("bolsas", "🗂 <b>Bolsas y listas de espera</b>", 230),
    ("seguimiento", "🔎 <b>Seguimiento de procesos</b>", 150),
]


def clasificar(texto):
    t = norm(texto)
    if RE_SEGUIMIENTO.search(t):
        return "seguimiento"
    if RE_BOLSA.search(t):
        return "bolsas"
    if RE_OFERTA.search(t):
        return "ofertas"
    if RE_CONVOCATORIA.search(t):
        return "convocatorias"
    return "seguimiento"


RE_FECHA_TITULO = re.compile(
    r"^(Resolución|Anuncio|Orden|Acuerdo|Edicto|Decreto)( \S+)? de \d{1,2}º? de \w+ de \d{4},?\s*",
    re.I,
)
RE_ORGANO = re.compile(r"^(?:de la|del|de los|de las|de)\s+([^,]+),\s*", re.I)
RE_POR_LA_QUE = re.compile(r"^por (la|el) que\s+", re.I)


def limpiar_titulo(titulo):
    """Quita 'Resolución de X de mes de año, del órgano, por la que'.

    Devuelve (órgano, resto del título)."""
    t = " ".join(titulo.split())
    organo = ""
    nuevo = RE_FECHA_TITULO.sub("", t, count=1)
    if nuevo != t:
        t = nuevo
        m = RE_ORGANO.match(t)
        if m:
            organo = m.group(1).strip()
            t = t[m.end():]
        t = RE_POR_LA_QUE.sub("", t, count=1)
    t = t.strip()
    if t:
        t = t[0].upper() + t[1:]
    return organo, t


MINUSCULAS = {"de", "del", "la", "las", "los", "y", "e", "el", "en", "a"}


def nombre_propio(mayusculas):
    """'AYUNTAMIENTO DE LOGROSÁN' -> 'Ayuntamiento de Logrosán'."""
    palabras = mayusculas.strip().lower().split()
    salida = []
    for i, p in enumerate(palabras):
        salida.append(p if (i and p in MINUSCULAS) else p[:1].upper() + p[1:])
    return " ".join(salida)


def recortar(texto, limite):
    if len(texto) <= limite:
        return texto
    return texto[:limite].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


# ----------------------------------------------------------------------- DOE

RE_CABECERA_DOE = re.compile(r"^([^a-záéíóúñü]+?)\s*\.\s*(.*)$", re.S)


def leer_doe():
    raiz = ET.fromstring(descargar(DOE_RSS))
    items = []
    for it in raiz.iter("item"):
        enlace = (it.findtext("link") or "").strip().replace("http://", "https://")
        titulo = html.unescape(it.findtext("title") or "").strip()
        desc = html.unescape(it.findtext("description") or "").strip()
        seccion = (it.findtext("category") or "").strip()
        if not enlace or not titulo:
            continue

        cabecera, sep, _ = desc.partition(".- ")
        organismo, descriptor = "", ""
        if sep:
            m = RE_CABECERA_DOE.match(cabecera)
            if m:
                organismo, descriptor = m.group(1), m.group(2)
            else:
                organismo = cabecera

        texto = norm(descriptor + " " + titulo)
        es_seccion_2 = seccion.upper().startswith("II.")
        if not (RE_EMPLEO.search(texto)
                or (es_seccion_2 and RE_EMPLEO_SEC2.search(texto))):
            continue

        _, resto = limpiar_titulo(titulo)
        items.append({
            "id": enlace,
            "fuente": "DOE",
            "organismo": nombre_propio(organismo) if organismo else "DOE",
            "titulo": resto,
            "url": enlace,
            "grupo": clasificar(descriptor + " " + titulo),
        })
    return items


# ----------------------------------------------------------------------- BOE

def leer_boe(fecha):
    try:
        datos = descargar(BOE_API.format(fecha=fecha), accept="application/xml")
    except urllib.error.HTTPError as e:
        if e.code == 404:  # ese día no hay BOE (domingos) o aún no ha salido
            return []
        raise
    raiz = ET.fromstring(datos)
    items = []
    for seccion in raiz.iter("seccion"):
        if not (seccion.get("codigo") or "").upper().startswith("2"):
            continue  # solo II. Autoridades y personal (2A y 2B)
        for dep in seccion.iter("departamento"):
            nombre_dep = dep.get("nombre") or ""
            for it in dep.iter("item"):
                titulo = " ".join((it.findtext("titulo") or "").split())
                if not titulo:
                    continue
                if not RE_EXTREMADURA.search(norm(nombre_dep + " " + titulo)):
                    continue
                ident = (it.findtext("identificador") or "").strip()
                url = (it.findtext("url_html") or it.findtext("url_pdf") or "").strip()
                if not url:
                    continue
                organo, resto = limpiar_titulo(titulo)
                items.append({
                    "id": ident or url,
                    "fuente": "BOE",
                    "organismo": organo or nombre_propio(nombre_dep),
                    "titulo": resto,
                    "url": url,
                    "grupo": clasificar(titulo),
                })
    return items


# ------------------------------------------------------------------ Telegram

def enviar(texto):
    datos = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=datos), timeout=40) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram respondió {e.code}: {e.read().decode(errors='replace')}")


def componer(items, hoy):
    fecha = f"{DIAS[hoy.weekday()]} {hoy.day} de {MESES[hoy.month - 1]}"
    lineas = [f"📋 <b>Empleo público · Extremadura</b>\n{fecha} · {len(items)} novedades"]
    for clave, cabecera, limite in GRUPOS:
        grupo = [i for i in items if i["grupo"] == clave]
        if not grupo:
            continue
        lineas.append(f"\n{cabecera} ({len(grupo)})")
        for i in grupo:
            lineas.append(
                f"• <b>{html.escape(i['organismo'])}</b>: "
                f"{html.escape(recortar(i['titulo'], limite))} "
                f"<a href=\"{html.escape(i['url'], quote=True)}\">{i['fuente']}</a>"
            )
    return lineas


def trocear(lineas, limite=3800):
    """Telegram admite 4096 caracteres por mensaje."""
    mensajes, actual = [], ""
    for linea in lineas:
        if actual and len(actual) + len(linea) + 1 > limite:
            mensajes.append(actual)
            actual = linea.lstrip("\n")
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        mensajes.append(actual)
    return mensajes


# ---------------------------------------------------------------------- main

def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")

    hoy = datetime.now(ZoneInfo("Europe/Madrid"))
    vistos = []
    if ESTADO.exists():
        vistos = json.loads(ESTADO.read_text(encoding="utf-8")).get("vistos", [])
    ya = set(vistos)

    items, errores = [], []
    for nombre, lector in (("DOE", leer_doe),
                           ("BOE", lambda: leer_boe(hoy.strftime("%Y%m%d")))):
        try:
            encontrados = lector()
            print(f"{nombre}: {len(encontrados)} anuncios de empleo público")
            items += encontrados
        except Exception as e:  # una fuente caída no debe tumbar la otra
            print(f"ERROR {nombre}: {e!r}")
            errores.append(nombre)

    nuevos, ids = [], set()
    for i in items:
        if i["id"] not in ya and i["id"] not in ids:
            ids.add(i["id"])
            nuevos.append(i)
    print(f"Nuevos: {len(nuevos)}")

    if nuevos:
        for mensaje in trocear(componer(nuevos, hoy)):
            enviar(mensaje)
        vistos += [i["id"] for i in nuevos]
        ESTADO.write_text(
            json.dumps({"vistos": vistos[-MAX_VISTOS:]}, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
    elif AVISAR_SI_VACIO:
        aviso = "📋 Empleo público · Extremadura: sin novedades desde el último aviso."
        if errores:
            aviso += f"\n⚠️ No se pudo leer: {', '.join(errores)}."
        enviar(aviso)

    if len(errores) == 2:
        sys.exit("No se pudo leer ninguna fuente")


if __name__ == "__main__":
    main()

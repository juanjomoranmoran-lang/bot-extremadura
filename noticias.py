#!/usr/bin/env python3
"""Resumen de noticias de Extremadura para Telegram.

1. Recoge titulares de las últimas horas (Google News, que agrega Hoy,
   El Periódico Extremadura, Canal Extremadura, Europa Press, etc.).
2. Agrupa los que cuentan la misma noticia: cuantos más medios la dan,
   más relevante se considera.
3. Groq elige las 5-8 más importantes y redacta una línea de contexto.
   Si Groq falla, envía las más repetidas sin contexto.
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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "")  # vacío = elegir automáticamente

ESTADO = Path("noticias_estado.json")
UA = "Mozilla/5.0 (compatible; bot-extremadura/1.0)"

# Búsquedas en Google News (España, en español). "when:1d" = últimas 24 h.
BUSQUEDAS = [
    "Extremadura when:1d",
    "Junta de Extremadura when:1d",
    "Badajoz when:1d",
    "Cáceres when:1d",
    "Mérida Extremadura when:1d",
    "Plasencia OR Almendralejo OR \"Don Benito\" OR Zafra OR Navalmoral when:1d",
]
GNEWS = "https://news.google.com/rss/search?q={q}&hl=es&gl=ES&ceid=ES:es"

HORAS_VENTANA = 14      # antigüedad máxima de un titular
CANDIDATAS = 45         # noticias que se le pasan a la IA
MAX_NOTICIAS = 8        # noticias en el resumen
SIMILITUD = 0.5         # umbral para considerar que dos titulares son la misma noticia

MODELOS_PREFERIDOS = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.1-8b-instant",
]

INSTRUCCIONES = """Eres el editor de un boletín de noticias de Extremadura para un lector \
que quiere enterarse solo de lo importante de la región.

Recibirás una lista numerada de noticias. Cada una indica cuántos medios la publican y \
uno o varios titulares de distintos medios sobre el mismo hecho.

Elige las noticias relevantes para la región, 8 como máximo, y ordénalas de más a menos \
importante. Lo normal es elegir entre 5 y 8, pero no rellenes: si solo hay dos que merezcan \
la pena, devuelve dos, y si no hay ninguna, devuelve la lista vacía.

SÍ es relevante: política autonómica y decisiones de la Junta, economía, empleo y empresas, \
infraestructuras (tren, autovías, regadíos), sanidad, educación, energía, campo, incendios, \
meteorología adversa, sucesos graves, tribunales, y cualquier hecho que afecte a mucha gente.

NO es relevante: fiestas y actos locales menores, notas de prensa rutinarias, agenda cultural, \
declaraciones sin novedad, y deporte (salvo un hito excepcional).

Que una noticia salga en muchos medios es una buena señal de importancia, pero no la única.
No repitas el mismo hecho dos veces.
Da prioridad a lo que afecta a toda la región frente a lo que solo afecta a una ciudad, y \
procura que no haya más de tres noticias de una misma localidad.
Los avisos vigentes de meteorología adversa, incendios o cortes de carreteras van siempre \
entre las primeras.

Estilo: español de España, registro periodístico sobrio. Usa el pretérito perfecto para hechos \
recientes ("ha aprobado", no "aprobó"). No exageres: el titular no puede decir más de lo que \
dicen los titulares originales (una filtración de agua no es una inundación). Titular y \
contexto deben referirse al mismo hecho.

Para cada noticia elegida escribe:
- "titular": un titular claro y neutro, de 12 palabras como máximo.
- "contexto": una sola frase de 25 palabras como máximo que amplíe el titular usando \
ÚNICAMENTE datos presentes en los titulares recibidos. No inventes cifras, nombres ni causas. \
Si los titulares no dan para más, deja "contexto" vacío.

Responde solo con JSON, con este formato exacto:
{"noticias": [{"n": 3, "titular": "...", "contexto": "..."}]}
donde "n" es el número de la noticia en la lista."""

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

VACIAS = set("""de la el en y a los las del un una por con para que se su al lo es como mas
o pero sus le ya entre cuando muy sin sobre tambien hasta hay donde desde todo nos durante
uno ni contra ese eso ante esta este tras ha han sera son fue ser no si tiene esta estan
nuevo nueva nuevos hoy ayer manana ano anos dia dias extremadura extremeno extremena
extremenos extremenas""".split())


# ---------------------------------------------------------------- utilidades

def norm(texto):
    t = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def palabras(titulo):
    return {p for p in re.findall(r"[a-z0-9]+", norm(titulo))
            if len(p) > 2 and p not in VACIAS}


def parecido(a, b):
    """Proporción de palabras compartidas respecto al titular más corto.
    Exige al menos 3 palabras en común para no juntar noticias distintas."""
    comunes = len(a & b)
    if comunes < 3:
        return 0.0
    return comunes / min(len(a), len(b))


def descargar(url, datos=None, cabeceras=None):
    c = {"User-Agent": UA}
    c.update(cabeceras or {})
    req = urllib.request.Request(url, data=datos, headers=c)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


# ------------------------------------------------------------------ recogida

def recoger(ahora):
    limite = ahora - timedelta(hours=HORAS_VENTANA)
    vistos, titulares, fallos = set(), [], 0
    for busqueda in BUSQUEDAS:
        url = GNEWS.format(q=urllib.parse.quote(busqueda))
        try:
            raiz = ET.fromstring(descargar(url))
        except Exception as e:
            print(f"ERROR en búsqueda '{busqueda}': {e!r}")
            fallos += 1
            continue
        n = 0
        for it in raiz.iter("item"):
            titulo = html.unescape(it.findtext("title") or "").strip()
            enlace = (it.findtext("link") or "").strip()
            medio = html.unescape(it.findtext("source") or "").strip()
            if not titulo or not enlace:
                continue
            # Google News añade " - Nombre del medio" al final del titular.
            if medio and titulo.endswith(" - " + medio):
                titulo = titulo[: -len(" - " + medio)].strip()
            try:
                fecha = parsedate_to_datetime(it.findtext("pubDate") or "")
            except (TypeError, ValueError):
                continue
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)
            if fecha < limite:
                continue
            clave = (norm(titulo), norm(medio))
            if clave in vistos:
                continue
            vistos.add(clave)
            titulares.append({"titulo": titulo, "url": enlace, "medio": medio or "Medio",
                              "fecha": fecha, "palabras": palabras(titulo)})
            n += 1
        print(f"'{busqueda}': {n} titulares nuevos")
    if fallos == len(BUSQUEDAS):
        raise RuntimeError("No se pudo leer ninguna búsqueda de noticias")
    return titulares


def agrupar(titulares):
    """Junta los titulares que hablan de lo mismo."""
    grupos = []
    for t in sorted(titulares, key=lambda x: x["fecha"]):
        mejor, mejor_s = None, SIMILITUD
        for g in grupos:
            s = max(parecido(t["palabras"], o["palabras"]) for o in g)
            if s >= mejor_s:
                mejor, mejor_s = g, s
        if mejor is not None:
            mejor.append(t)
        else:
            grupos.append([t])
    return grupos


def medios(grupo):
    return len({norm(t["medio"]) for t in grupo})


# ------------------------------------------------------------------------ IA

def modelos_a_probar():
    """Modelos de Groq a intentar, por orden de preferencia."""
    if GROQ_MODEL:
        return [GROQ_MODEL]
    try:
        datos = json.loads(descargar("https://api.groq.com/openai/v1/models",
                                     cabeceras={"Authorization": f"Bearer {GROQ_KEY}"}))
        disponibles = {m["id"] for m in datos.get("data", [])}
        elegidos = [m for m in MODELOS_PREFERIDOS if m in disponibles]
        if elegidos:
            return elegidos
    except Exception as e:
        print(f"No se pudo consultar la lista de modelos: {e!r}")
    return MODELOS_PREFERIDOS[:2]


def extraer_json(texto):
    """Saca el objeto JSON de la respuesta aunque venga con texto alrededor."""
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fin <= inicio:
        raise ValueError("la respuesta no contiene JSON")
    return json.loads(texto[inicio:fin + 1])


def preguntar(modelo, lista):
    cuerpo = json.dumps({
        "model": modelo,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": INSTRUCCIONES},
            {"role": "user", "content": lista},
        ],
    }).encode()
    respuesta = json.loads(descargar(
        "https://api.groq.com/openai/v1/chat/completions", datos=cuerpo,
        cabeceras={"Authorization": f"Bearer {GROQ_KEY}",
                   "Content-Type": "application/json"}))
    contenido = extraer_json(respuesta["choices"][0]["message"]["content"] or "")
    if not isinstance(contenido.get("noticias"), list):
        raise ValueError("falta la lista 'noticias'")
    return contenido["noticias"]


def seleccionar_con_ia(grupos):
    lineas = []
    for n, g in enumerate(grupos, 1):
        distintos = []
        for t in sorted(g, key=lambda x: -len(x["titulo"])):
            if t["titulo"] not in distintos:
                distintos.append(t["titulo"])
        lineas.append(f"[{n}] ({medios(g)} medios) " + " | ".join(distintos[:3]))
    lista = "\n".join(lineas)

    noticias = None
    for modelo in modelos_a_probar():
        for intento in (1, 2):
            try:
                noticias = preguntar(modelo, lista)
                print(f"Modelo de Groq: {modelo} (intento {intento})")
                break
            except Exception as e:
                detalle = ""
                if isinstance(e, urllib.error.HTTPError):
                    detalle = e.read().decode(errors="replace")[:300]
                print(f"Fallo con {modelo}, intento {intento}: {e!r} {detalle}")
        if noticias is not None:
            break
    if noticias is None:
        raise RuntimeError("Ningún modelo de Groq ha dado una respuesta válida")

    elegidas, usados = [], set()
    for x in noticias:
        try:
            n = int(x["n"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 1 <= n <= len(grupos) or n in usados:
            continue
        usados.add(n)
        elegidas.append({
            "grupo": grupos[n - 1],
            "titular": str(x.get("titular") or "").strip(),
            "contexto": str(x.get("contexto") or "").strip(),
        })
    return elegidas[:MAX_NOTICIAS]


# ------------------------------------------------------------------ Telegram

def enviar(texto):
    datos = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        descargar(f"https://api.telegram.org/bot{TOKEN}/sendMessage", datos=datos)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram respondió {e.code}: {e.read().decode(errors='replace')}")


def componer(elegidas, ahora, con_ia):
    momento = "de la mañana" if ahora.hour < 14 else "de la noche"
    fecha = f"{DIAS[ahora.weekday()]} {ahora.day} de {MESES[ahora.month - 1]}"
    partes = [f"🗞 <b>Extremadura · resumen {momento}</b>\n{fecha}"]
    for n, e in enumerate(elegidas, 1):
        g = e["grupo"]
        principal = max(g, key=lambda t: t["fecha"])
        titular = e["titular"] or principal["titulo"]
        bloque = f"\n<b>{n}. {html.escape(titular)}</b>"
        if e["contexto"]:
            bloque += f"\n{html.escape(e['contexto'])}"
        pie = f"<a href=\"{html.escape(principal['url'], quote=True)}\">{html.escape(principal['medio'])}</a>"
        if medios(g) > 1:
            pie += f" · {medios(g)} medios"
        partes.append(f"{bloque}\n{pie}")
    if not con_ia:
        partes.append("\n<i>Sin resumen de IA en esta edición: se muestran las noticias más repetidas.</i>")
    return "\n".join(partes)


# ---------------------------------------------------------------------- main

def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")

    ahora = datetime.now(ZoneInfo("Europe/Madrid"))
    enviados = []
    if ESTADO.exists():
        enviados = json.loads(ESTADO.read_text(encoding="utf-8")).get("enviados", [])
    ya = [palabras(t) for t in enviados]

    grupos = agrupar(recoger(ahora))
    # Fuera lo que ya se contó en un resumen anterior.
    grupos = [g for g in grupos
              if not any(parecido(t["palabras"], v) >= SIMILITUD for t in g for v in ya)]
    grupos.sort(key=lambda g: (medios(g), max(t["fecha"] for t in g)), reverse=True)
    grupos = grupos[:CANDIDATAS]
    print(f"Noticias candidatas: {len(grupos)}")

    if not grupos:
        print("Nada nuevo que contar.")
        return

    con_ia = False
    elegidas = None
    if GROQ_KEY:
        try:
            elegidas = seleccionar_con_ia(grupos)
            con_ia = True
        except Exception as e:
            print(f"ERROR de Groq: {e!r}")
    else:
        print("No hay GROQ_API_KEY: se envía sin IA.")
    if elegidas is None:
        elegidas = [{"grupo": g, "titular": "", "contexto": ""} for g in grupos[:MAX_NOTICIAS]]

    if not elegidas:
        print("La IA no ha encontrado nada relevante: no se envía resumen.")
        return

    enviar(componer(elegidas, ahora, con_ia))

    for e in elegidas:
        enviados.append(max(e["grupo"], key=lambda t: len(t["titulo"]))["titulo"])
    ESTADO.write_text(json.dumps({"enviados": enviados[-300:]}, ensure_ascii=False, indent=0),
                      encoding="utf-8")


if __name__ == "__main__":
    main()

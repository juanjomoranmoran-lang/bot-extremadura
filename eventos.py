#!/usr/bin/env python3
"""Agenda semanal de eventos de Extremadura para Telegram.

- Lee la agenda pública de Viral Agenda (viralagenda.com) para Extremadura:
  la portada (todo lo de los próximos días) y las categorías de turismo,
  gastronomía, tradición, festivales y conciertos (con más antelación).
- Envía lo de este fin de semana por provincia y lo destacado de las dos
  semanas siguientes.
- Avisa una sola vez, con más de un mes de margen, de las grandes citas
  anuales (lista CITAS_GRANDES, editable).

No usa IA ni librerías externas.
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
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

ESTADO = Path("eventos_estado.json")
UA = "Mozilla/5.0 (compatible; bot-extremadura/1.0)"
BASE = "https://www.viralagenda.com"
AGENDA = BASE + "/es/extremadura"

# Páginas que se leen. La portada trae de todo pero solo unos días;
# las categorías llegan más lejos en el calendario.
PAGINAS = ["", "/tourism", "/gastronomy", "/tradition", "/festivals", "/concerts"]

# Categorías que más interesan: van primero y son las únicas de "Más adelante".
DESTACADAS = ["turismo", "gastronomia", "tradicion", "mercados y ferias",
              "festivales", "naturaleza"]
# Categorías que no se envían nunca.
EXCLUIDAS = {"academicos", "formacion", "conferencias", "bienestar", "otras",
             "literatura", "cine y video", "solidaridad"}

MAX_POR_PROVINCIA = 14
MAX_MAS_ADELANTE = 15
DIAS_MAS_ADELANTE = 14

# Grandes citas anuales: (nombre, lugar, mes habitual, cuándo suele ser).
# El bot avisa una vez, unas semanas antes de que empiece ese mes.
# Las fechas exactas cambian cada año: el aviso es para ir mirándolas.
CITAS_GRANDES = [
    ("Jarramplas", "Piornal", 1, "19 y 20 de enero"),
    ("Las Carantoñas", "Acehúche", 1, "20 y 21 de enero"),
    ("Las Candelas", "Almendralejo", 2, "primeros de febrero"),
    ("Ruta del Emperador Carlos V", "La Vera", 2, "primeros de febrero"),
    ("Feria Internacional de Ornitología (FIO)", "Monfragüe", 2, "finales de febrero"),
    ("Carnaval de Badajoz", "Badajoz", 2, "febrero o marzo, según el año"),
    ("Cerezo en Flor", "Valle del Jerte", 3, "de mediados de marzo a primeros de abril"),
    ("Semana Santa", "Cáceres, Mérida, Badajoz y Jerez de los Caballeros", 3, "marzo o abril, según el año"),
    ("Los Empalaos", "Valverde de la Vera", 3, "noche del Jueves Santo"),
    ("Feria Nacional del Queso", "Trujillo", 4, "finales de abril o primeros de mayo"),
    ("WOMAD", "Cáceres", 5, "un fin de semana de mayo"),
    ("Batalla de La Albuera", "La Albuera", 5, "mediados de mayo"),
    ("Salón del Jamón Ibérico", "Jerez de los Caballeros", 5, "mayo"),
    ("Emerita Lvdica", "Mérida", 5, "finales de mayo o junio"),
    ("Sanjuanes", "Coria", 6, "del 23 al 29 de junio"),
    ("Festival Internacional de Teatro Clásico", "Mérida", 7, "julio y agosto"),
    ("Festival Templario", "Jerez de los Caballeros", 7, "julio"),
    ("Martes Mayor", "Plasencia", 8, "primer martes de agosto"),
    ("Fiestas de la Vendimia", "Almendralejo", 8, "mediados de agosto"),
    ("Feria Internacional Ganadera", "Zafra", 9, "finales de septiembre y primeros de octubre"),
    ("Otoño Mágico", "Valle del Ambroz", 11, "fines de semana de noviembre"),
    ("La Encamisá", "Torrejoncillo", 12, "7 de diciembre"),
    ("Los Escobazos", "Jarandilla de la Vera", 12, "7 de diciembre"),
]

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
MES_ABREV = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS_SEMANA = {"lun", "mar", "mie", "jue", "vie", "sab", "dom"}
DIAS_CORTO = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def norm(texto):
    t = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn").strip()


def descargar(url, datos=None):
    req = urllib.request.Request(url, data=datos, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


# ------------------------------------------------------------------- lectura

class Aplanador(HTMLParser):
    """Convierte la página en una lista plana de palabras y enlaces, en orden.

    Así el lector no depende de las clases CSS de la web, solo del orden en
    que aparece la información: fecha, evento, hora, lugar y categorías."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elementos = []      # ("p", palabra) o ("a", href, title, texto)
        self._enlace = None
        self._saltar = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._saltar += 1
        elif tag == "a":
            a = dict(attrs)
            self._enlace = [a.get("href") or "", a.get("title") or "", []]

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._saltar:
            self._saltar -= 1
        elif tag == "a" and self._enlace is not None:
            href, title, trozos = self._enlace
            self.elementos.append(("a", href, title, " ".join(" ".join(trozos).split())))
            self._enlace = None

    def handle_data(self, data):
        if self._saltar:
            return
        if self._enlace is not None:
            self._enlace[2].append(data)
        else:
            for palabra in data.split():
                self.elementos.append(("p", palabra))


RE_EVENTO = re.compile(r"/es/events/(\d+)/")
RE_LOCALIDAD = re.compile(r"/es/extremadura/(badajoz|caceres)/[^/?#]+/?$")
RE_CATEGORIA = re.compile(r"/es/extremadura/([a-z-]+)/?(?:\?.*)?$")
RE_LOCAL = re.compile(r"/es/p/")
RE_HORA = re.compile(r"^\d{1,2}:\d{2}$")
RE_SUFIJO = re.compile(r"\s*\((municipio|ciudad)\)\s*$|\s*-\s*municipio\s*$", re.I)


def mes_de(palabra):
    p = norm(palabra).strip(".,")
    if p in MES_ABREV:
        return MES_ABREV.index(p) + 1
    if p in MESES:
        return MESES.index(p) + 1
    return None


def fecha_proxima(dia, mes, hoy, anio=None):
    """Fecha más cercana en el futuro para un día y mes sin año."""
    try:
        if anio:
            return date(anio, mes, dia)
        f = date(hoy.year, mes, dia)
        if f < hoy - timedelta(days=60):
            f = date(hoy.year + 1, mes, dia)
        return f
    except ValueError:
        return None


def leer_pagina(contenido, hoy):
    ap = Aplanador()
    ap.feed(contenido)
    el = ap.elementos
    eventos, actual = {}, None
    fecha, hasta = None, None

    def palabra(i):
        return el[i][1] if 0 <= i < len(el) and el[i][0] == "p" else ""

    def dia_mes(i):
        """¿Hay 'DD MES' en las posiciones i, i+1? Devuelve (día, mes) o None."""
        d, m = palabra(i), mes_de(palabra(i + 1))
        if d.isdigit() and 1 <= int(d) <= 31 and m:
            return int(d), m
        return None

    i = 0
    while i < len(el):
        e = el[i]
        if e[0] == "p":
            p = norm(e[1])
            if p in DIAS_SEMANA and dia_mes(i + 1):
                d, m = dia_mes(i + 1)
                anio = palabra(i + 3)
                fecha = fecha_proxima(d, m, hoy, int(anio) if re.fullmatch(r"20\d\d", anio) else None)
                hasta = None
                i += 3
                continue
            if p == "hasta" and dia_mes(i + 1):
                d, m = dia_mes(i + 1)
                hasta = fecha_proxima(d, m, hoy)
                if hasta and fecha and hasta < fecha:
                    hasta = fecha_proxima(d, m, hoy, fecha.year + 1)
                i += 3
                continue
            if actual and not actual["hora"] and RE_HORA.match(e[1]) and e[1] != "00:00":
                actual["hora"] = e[1]
        else:
            _, href, title, texto = e
            m = RE_EVENTO.search(href)
            if m and fecha:
                ident = m.group(1)
                nombre = html.unescape(title or texto).strip()
                if actual is None or actual["id"] != ident:
                    actual = eventos.get(ident)
                    if actual is None:
                        actual = eventos[ident] = {
                            "id": ident, "titulo": nombre, "inicio": fecha,
                            "fin": hasta or fecha, "hora": "", "zona": "",
                            "provincia": "", "local": "", "categorias": [],
                            "url": urllib.parse.urljoin(BASE, href.split("?")[0]),
                        }
                    hasta = None
                if nombre and not nombre.startswith("/") and (
                        not actual["titulo"] or actual["titulo"].startswith("/")):
                    actual["titulo"] = nombre
            elif actual and texto:
                loc = RE_LOCALIDAD.search(href)
                cat = RE_CATEGORIA.search(href)
                if loc:
                    actual["zona"], actual["provincia"] = texto, loc.group(1)
                elif cat and cat.group(1) not in ("badajoz", "caceres"):
                    if texto not in actual["categorias"]:
                        actual["categorias"].append(texto)
                elif RE_LOCAL.search(href) and not actual["local"]:
                    actual["local"] = texto
        i += 1
    return list(eventos.values())


def recoger(hoy):
    todos, fallos = {}, 0
    for pagina in PAGINAS:
        try:
            encontrados = leer_pagina(descargar(AGENDA + pagina).decode("utf-8", "replace"), hoy)
        except Exception as e:
            print(f"ERROR leyendo {AGENDA + pagina}: {e!r}")
            fallos += 1
            continue
        print(f"{AGENDA + pagina}: {len(encontrados)} eventos")
        for ev in encontrados:
            previo = todos.get(ev["id"])
            if previo is None:
                todos[ev["id"]] = ev
            else:   # mismo evento visto en otra página: quedarse con el rango más amplio
                previo["inicio"] = min(previo["inicio"], ev["inicio"])
                previo["fin"] = max(previo["fin"], ev["fin"])
                for c in ev["categorias"]:
                    if c not in previo["categorias"]:
                        previo["categorias"].append(c)
    if fallos == len(PAGINAS):
        raise RuntimeError("No se pudo leer ninguna página de la agenda")
    return list(todos.values())


# ----------------------------------------------------------------- selección

def localidad(ev):
    """Municipio si se conoce; si no, la zona o comarca."""
    if RE_SUFIJO.search(ev["local"]):
        return RE_SUFIJO.sub("", ev["local"]).strip()
    return ev["zona"] or ev["local"] or "Extremadura"


def prioridad(ev):
    cats = [norm(c) for c in ev["categorias"]]
    mejores = [DESTACADAS.index(c) for c in cats if c in DESTACADAS]
    return min(mejores) if mejores else len(DESTACADAS)


RE_ANULADO = re.compile(r"cancelad|aplazad|suspendid|anulad")


def admitido(ev):
    if RE_ANULADO.search(norm(ev["titulo"])):
        return False
    cats = {norm(c) for c in ev["categorias"]}
    return not cats or bool(cats - EXCLUIDAS)


def fecha_corta(f):
    return f"{DIAS_CORTO[f.weekday()]} {f.day}"


def linea(ev, hoy, con_mes=False):
    inicio, fin = ev["inicio"], ev["fin"]
    mes_ini, mes_fin = MES_ABREV[inicio.month - 1], MES_ABREV[fin.month - 1]
    if fin > inicio and inicio < hoy:            # ya empezó: importa cuándo acaba
        cuando = f"hasta el {fecha_corta(fin)} {mes_fin}"
    elif fin > inicio and inicio.month != fin.month:
        cuando = f"{fecha_corta(inicio)} {mes_ini}–{fin.day} {mes_fin}"
    elif fin > inicio:
        cuando = f"{fecha_corta(inicio)}–{fin.day} {mes_fin}"
    else:
        cuando = fecha_corta(inicio) + (f" {mes_ini}" if con_mes else "")
        if ev["hora"]:
            cuando += f", {ev['hora']}"
    titulo = ev["titulo"] if len(ev["titulo"]) <= 90 else ev["titulo"][:88].rstrip() + "…"
    cats = [c for c in ev["categorias"] if norm(c) not in EXCLUIDAS][:2]
    etiqueta = f" <i>({html.escape(', '.join(cats))})</i>" if cats else ""
    return (f"• {cuando} · <b>{html.escape(localidad(ev))}</b>: "
            f"<a href=\"{html.escape(ev['url'], quote=True)}\">{html.escape(titulo)}</a>{etiqueta}")


def citas_por_avisar(hoy, avisadas):
    """Grandes citas cuyo mes habitual empieza en menos de 45 días."""
    salida = []
    for nombre, lugar, mes, cuando in CITAS_GRANDES:
        anio = hoy.year if mes > hoy.month else hoy.year + 1
        dias = (date(anio, mes, 1) - hoy).days
        clave = f"{nombre}|{anio}"
        if 0 < dias <= 45 and clave not in avisadas:
            salida.append((clave, f"• <b>{html.escape(nombre)}</b> ({html.escape(lugar)}): "
                                  f"suele ser {html.escape(cuando)}. Fechas de {anio} por confirmar."))
    return salida


def componer(eventos, hoy, citas):
    domingo = hoy + timedelta(days=(6 - hoy.weekday()) % 7)
    tope = domingo + timedelta(days=DIAS_MAS_ADELANTE)
    eventos = [e for e in eventos if admitido(e)]

    finde = [e for e in eventos if e["inicio"] <= domingo and e["fin"] >= hoy]
    luego = [e for e in eventos if domingo < e["inicio"] <= tope and prioridad(e) < len(DESTACADAS)]

    if hoy == domingo:
        rango = f"hoy, domingo {hoy.day} de {MESES[hoy.month - 1]}"
    else:
        rango = f"del {fecha_corta(hoy)} al {fecha_corta(domingo)} de {MESES[domingo.month - 1]}"
    lineas = [f"🎪 <b>Agenda de Extremadura</b>\n{rango}"]

    if citas:
        lineas.append("\n⭐ <b>Para ir reservando</b>")
        lineas += [texto for _, texto in citas]

    for clave, nombre in (("badajoz", "Badajoz"), ("caceres", "Cáceres"), ("", "Otras zonas")):
        suyos = sorted((e for e in finde if e["provincia"] == clave),
                       key=lambda e: (prioridad(e), max(e["inicio"], hoy), e["hora"] or "99"))
        if not suyos:
            continue
        lineas.append(f"\n📍 <b>{nombre}</b> ({len(suyos)})")
        lineas += [linea(e, hoy) for e in suyos[:MAX_POR_PROVINCIA]]
        if len(suyos) > MAX_POR_PROVINCIA:
            lineas.append(f"   … y {len(suyos) - MAX_POR_PROVINCIA} más en "
                          f"<a href=\"{AGENDA}\">la agenda completa</a>")

    if luego:
        luego.sort(key=lambda e: (e["inicio"], prioridad(e)))
        lineas.append("\n🗓 <b>Más adelante</b> (ferias, fiestas, gastronomía y turismo)")
        lineas += [linea(e, hoy, con_mes=True) for e in luego[:MAX_MAS_ADELANTE]]

    if len(lineas) == 1:
        return None
    lineas.append(f"\n<i>Fuente: <a href=\"{AGENDA}\">Viral Agenda</a></i>")
    return lineas


def trocear(lineas, limite=3800):
    mensajes, actual = [], ""
    for l in lineas:
        if actual and len(actual) + len(l) + 1 > limite:
            mensajes.append(actual)
            actual = l.lstrip("\n")
        else:
            actual = f"{actual}\n{l}" if actual else l
    if actual:
        mensajes.append(actual)
    return mensajes


def enviar(texto):
    datos = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        descargar(f"https://api.telegram.org/bot{TOKEN}/sendMessage", datos=datos)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram respondió {e.code}: {e.read().decode(errors='replace')}")


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID")
    hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()

    avisadas = []
    if ESTADO.exists():
        avisadas = json.loads(ESTADO.read_text(encoding="utf-8")).get("citas_avisadas", [])

    eventos = recoger(hoy)
    con_fecha = sum(1 for e in eventos if e["provincia"])
    print(f"Eventos distintos: {len(eventos)} ({con_fecha} con provincia identificada)")

    citas = citas_por_avisar(hoy, set(avisadas))
    lineas = componer(eventos, hoy, citas)
    if lineas is None:
        print("No hay nada que enviar.")
        return
    for mensaje in trocear(lineas):
        enviar(mensaje)

    if citas:
        avisadas += [clave for clave, _ in citas]
        ESTADO.write_text(json.dumps({"citas_avisadas": avisadas[-200:]}, ensure_ascii=False, indent=0),
                          encoding="utf-8")


if __name__ == "__main__":
    main()

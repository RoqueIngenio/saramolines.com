"""Robot de la web saramolines.com.

Lo ejecutará GitHub Actions antes de publicar (ver .github/workflows/publicar.yml).
Copia la web a _site/ y, encima, mete el contenido vivo de Notion (página «SaraMolines.es»):

  1. Textos de la web  ← «BBDD. Textos de la web (Sara Molines)»
  2. Testimonios       ← «BBDD. Testimonios (Sara Molines)»: los marcados como Visible y con texto
  3. Artículos         ← «BBDD. Artículos (Sara Molines)»: los que están en Aprobado o Publicado
                         y cuya «Fecha programada» ya ha llegado

Con eso genera la página de cada artículo, el listado del blog con su buscador, los últimos
artículos de la portada, las preguntas frecuentes para Google, sitemap.xml, el feed RSS,
llms.txt y la página 404.

Seguridad:
  - Cada petición se reintenta hasta tres veces. Si aun así Notion no responde, el robot se
    detiene sin publicar y la web se queda como estaba.
  - Si alguna página conserva un dato marcado como pendiente (class="pendiente"), en GitHub
    el robot se niega a publicar.
El resultado de cada ejecución queda en _site/robot-estado.json.

Uso en este Mac (vista previa, sin publicar nada):
  python3 robot/construir.py            → genera _site/ con lo que hay (sin clave, usa textos.js)
  python3 robot/construir.py --prueba   → añade un artículo inventado para ver el diseño del blog

Es una adaptación del robot de roqueperez.es. Solo usa la biblioteca estándar de Python.
"""

import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

RAIZ = Path(__file__).resolve().parent.parent
SALIDA = RAIZ / "_site"
DOMINIO = "https://saramolines.com"
AUTORA = "Sara Molines"
BLOG = "blog"  # carpeta y dirección del blog; el nombre visible se cambia en los textos (blog.titulo)
RESERVAR = "https://wa.link/obq01p"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_VERSION = "2025-09-03"
DS_TEXTOS = "f65d6e80-1a62-48ea-a8c4-65a1fa4fd0ec"
DS_ARTICULOS = "eafd41f0-b081-4c4e-93ed-ba4416235102"
DS_TESTIMONIOS = "2e9d66ad-3658-47af-99ec-5297d5719777"
OPINIONES_GOOGLE = "https://www.google.com/maps/search/?api=1&query=Sara+Molines+Psic%C3%B3loga+Alcoy"
EN_GITHUB = os.environ.get("MARCAR_PUBLICADOS") == "si"
PRUEBA = "--prueba" in sys.argv
NAVEGADOR = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

MESES_LARGOS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

estado = {"actualizado": dt.datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="seconds")}


def leer(ruta):
    return ruta.read_text(encoding="utf-8")


def escribir(ruta, texto):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(texto, encoding="utf-8")


# ---------------------------------------------------------------- utilidades

PASAJEROS = (429, 500, 502, 503, 504)  # fallos que suelen arreglarse solos al reintentar


def pedir(url, metodo="GET", datos=None, cabeceras=None, intentos=3, espera=30):
    """Hace una petición y devuelve la respuesta. Si el fallo es pasajero, lo reintenta."""
    cuerpo = json.dumps(datos).encode() if datos is not None else None
    peticion = urllib.request.Request(url, data=cuerpo, method=metodo, headers=cabeceras or {})
    if cuerpo is not None:
        peticion.add_header("Content-Type", "application/json")
    if not peticion.has_header("User-agent"):
        peticion.add_header("User-Agent", NAVEGADOR)
    for intento in range(1, intentos + 1):
        try:
            with urllib.request.urlopen(peticion, timeout=espera) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in PASAJEROS or intento == intentos:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if intento == intentos:
                raise
        time.sleep(3 * intento)


def enlace_seguro(url):
    """Solo se publican enlaces http(s): nada de «javascript:» ni direcciones raras llegadas de Notion."""
    url = (url or "").strip()
    return url if url.lower().startswith(("https://", "http://")) else ""


def json_para_html(datos):
    """JSON listo para ir dentro de una etiqueta <script>, sin que un «</script>» en un texto pueda cerrarla."""
    return json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")


def notion(ruta, metodo="GET", datos=None):
    if not NOTION_TOKEN:
        raise RuntimeError("Falta el secreto NOTION_TOKEN")
    cabeceras = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NOTION_VERSION}
    try:
        return json.loads(pedir(f"https://api.notion.com/v1/{ruta}", metodo, datos, cabeceras))
    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Notion {e.code} en {ruta}: {detalle}") from None


def consultar(data_source, filtro=None):
    filas, cursor = [], None
    while True:
        datos = {"page_size": 100}
        if filtro:
            datos["filter"] = filtro
        if cursor:
            datos["start_cursor"] = cursor
        r = notion(f"data_sources/{data_source}/query", "POST", datos)
        filas += r["results"]
        if not r.get("has_more"):
            return filas
        cursor = r["next_cursor"]


def hijos(bloque):
    todos, cursor = [], None
    while True:
        ruta = f"blocks/{bloque}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        r = notion(ruta)
        todos += r["results"]
        if not r.get("has_more"):
            return todos
        cursor = r["next_cursor"]


def texto_plano(rich):
    return "".join(t.get("plain_text", "") for t in rich or [])


def a_marcado(rich):
    """Texto enriquecido de Notion → el formato de textos.js (** para la negrita)."""
    salida = ""
    for t in rich or []:
        s = t.get("plain_text", "")
        if not s.strip():
            salida += s
            continue
        inicio, nucleo, fin = re.match(r"^(\s*)(.*?)(\s*)$", s, re.S).groups()
        if t.get("annotations", {}).get("bold") and "**" not in nucleo:
            nucleo = f"**{nucleo}**"
        salida += inicio + nucleo + fin
    return salida


def a_html(rich):
    """Texto enriquecido de Notion → HTML seguro (para los artículos)."""
    salida = ""
    for t in rich or []:
        s = html.escape(t.get("plain_text", "")).replace("\n", "<br>")
        a = t.get("annotations", {})
        if a.get("bold"):
            s = f"<strong>{s}</strong>"
        if a.get("italic"):
            s = f"<em>{s}</em>"
        if a.get("strikethrough"):
            s = f"<s>{s}</s>"
        if a.get("underline"):
            s = f"<u>{s}</u>"
        enlace = enlace_seguro(t.get("href"))
        if enlace:
            propio = enlace.startswith(DOMINIO)
            s = f'<a href="{html.escape(enlace)}"' + ("" if propio else ' target="_blank" rel="noopener"') + f">{s}</a>"
        salida += s
    return salida


def propiedad(pagina, nombre):
    return pagina["properties"].get(nombre) or {}


def valor_texto(pagina, nombre):
    p = propiedad(pagina, nombre)
    return texto_plano(p.get(p.get("type"), [])) if p.get("type") in ("title", "rich_text") else ""


def valor_fecha(pagina, nombre):
    p = propiedad(pagina, nombre)
    if p.get("type") == "date" and p.get("date"):
        return p["date"]["start"][:10]
    return None


def a_slug(texto):
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:80] or "articulo"


def fecha_bonita(iso):
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} de {MESES_LARGOS[d.month - 1]} de {d.year}"


def reemplazar_entre(html_texto, nombre, nuevo):
    inicio, fin = f"<!-- ROBOT:{nombre}:INICIO -->", f"<!-- ROBOT:{nombre}:FIN -->"
    i, j = html_texto.index(inicio) + len(inicio), html_texto.index(fin)
    return html_texto[:i] + "\n" + nuevo + html_texto[j:]


# ---------------------------------------------------------------- textos

def leer_textos_js():
    bruto = leer(SALIDA / "textos.js")
    bruto = bruto[bruto.index("`") + 1: bruto.rindex("`")]
    textos, clave = {}, None
    for linea in bruto.split("\n"):
        etiqueta = re.match(r"^\[([\w.\-]+)\]$", linea.strip())
        if etiqueta:
            clave = etiqueta.group(1)
            textos[clave] = []
        elif clave and not linea.strip().startswith("##"):
            textos[clave].append(linea)
    return {k: "\n".join(v).strip() for k, v in textos.items()}


def textos_desde_notion():
    textos = {}
    for fila in consultar(DS_TEXTOS):
        clave = valor_texto(fila, "Clave").strip()
        p = propiedad(fila, "Texto")
        if clave and p.get("type") == "rich_text":
            texto = a_marcado(p["rich_text"]).strip()
            if texto:
                textos[clave] = texto
    return textos


def escribir_textos_js(textos):
    cabecera = leer(SALIDA / "textos.js")
    cabecera = cabecera[: cabecera.index("window.TEXTOS_WEB")]
    cuerpo = "".join(f"\n[{k}]\n{v.replace('`', chr(39))}\n" for k, v in textos.items())
    escribir(SALIDA / "textos.js", cabecera + "window.TEXTOS_WEB = `\n## Generado por el robot desde Notion. Edita los textos en Notion, no aquí.\n" + cuerpo + "`;\n")


def formatear(texto):
    texto = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", texto)
    return re.sub(r"==(.+?)==", r'<span class="resaltado">\1</span>', texto)


def sin_marcas(texto):
    return re.sub(r"\*\*|==", "", texto or "")


def texto_a_html(texto, formato):
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    if formato == "parrafos":
        return "".join(f"<p>{formatear(p.strip())}</p>" for p in re.split(r"\n\s*\n", texto) if p.strip())
    if formato == "lista":
        return "".join(f"<li>{formatear(l)}</li>" for l in lineas)
    return formatear(" ".join(lineas))


def volcar_textos(html_texto, textos):
    """Escribe los textos dentro del HTML, para que Google los lea sin JavaScript."""
    for m in list(re.finditer(r'<(\w+)[^>]*\sdata-texto="([^"]+)"[^>]*>', html_texto))[::-1]:
        etiqueta, clave = m.group(1), m.group(2)
        if not textos.get(clave):
            continue
        formato = re.search(r'data-formato="([^"]+)"', m.group(0))
        prof, j = 1, m.end()
        patron = re.compile(r"<(/?)" + etiqueta + r"\b[^>]*>")
        while prof:
            c = patron.search(html_texto, j)
            prof += -1 if c.group(1) else 1
            j = c.end()
        cierre = j - len(f"</{etiqueta}>")
        html_texto = html_texto[: m.end()] + texto_a_html(textos[clave], formato.group(1) if formato else None) + html_texto[cierre:]
    return html_texto


def faq_para_google(portada, textos):
    """Datos de «preguntas frecuentes» para Google, con las preguntas que hay en la portada."""
    preguntas = []
    for n in re.findall(r'data-texto="faq\.(\d+)\.pregunta"', portada):
        p, r = textos.get(f"faq.{n}.pregunta"), textos.get(f"faq.{n}.respuesta")
        if p and r:
            preguntas.append({"@type": "Question", "name": sin_marcas(p),
                              "acceptedAnswer": {"@type": "Answer", "text": sin_marcas(r)}})
    if not preguntas:
        return ""
    datos = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": preguntas}
    return f'  <script type="application/ld+json">{json_para_html(datos)}</script>\n  '


# ---------------------------------------------------------------- testimonios

def testimonios_desde_notion():
    """Opiniones marcadas como Visible y con texto, en el orden de la columna Orden."""
    opiniones = []
    for f in consultar(DS_TESTIMONIOS, {"property": "Visible", "checkbox": {"equals": True}}):
        nombre, texto = valor_texto(f, "Nombre").strip(), valor_texto(f, "Texto").strip()
        if nombre and texto:
            opiniones.append({"nombre": nombre, "texto": texto,
                              "origen": (propiedad(f, "Origen").get("select") or {}).get("name") or "",
                              "orden": propiedad(f, "Orden").get("number") or 0})
    opiniones.sort(key=lambda o: o["orden"])
    return opiniones


def testimonios_de_prueba():
    """Opiniones inventadas para revisar el diseño en este Mac. Nunca se publican."""
    return [{"nombre": f"Nombre {n}.", "origen": "Google", "orden": n,
             "texto": "Opinión de prueba para ver el diseño. " * veces}
            for n, veces in ((1, 2), (2, 4), (3, 1), (4, 3))]


def seccion_testimonios(opiniones):
    tarjetas = []
    for o in opiniones:
        parrafos = "".join(f"<p>{html.escape(p.strip())}</p>" for p in o["texto"].split("\n") if p.strip())
        origen = f'<span>Opinión en {html.escape(o["origen"])}</span>' if o["origen"] in ("Google", "Instagram") else ""
        tarjetas.append(f'          <figure class="testimonio">\n            <blockquote>{parrafos}</blockquote>\n'
                        f'            <figcaption>{html.escape(o["nombre"])}{origen}</figcaption>\n          </figure>')
    return ('    <section class="seccion seccion-menta" id="testimonios">\n      <div class="contenedor centrado aparece">\n'
            '        <p class="manuscrita" data-texto="testimonios.etiqueta">Vosotras</p>\n'
            '        <h2 data-texto="testimonios.titulo">Lo que cuentan quienes ya han dado el paso</h2>\n'
            '        <div class="testimonios-rejilla">\n' + "\n".join(tarjetas) + "\n        </div>\n"
            f'        <a class="testimonios-enlace" href="{OPINIONES_GOOGLE}" target="_blank" rel="noopener" '
            'data-texto="testimonios.enlace">Ver todas las opiniones en Google →</a>\n'
            "      </div>\n    </section>\n    ")


# ---------------------------------------------------------------- artículos

def bloques_a_html(bloques, carpeta_img, prefijo_img):
    partes, lista_abierta = [], None
    n_img = 0

    def cerrar_lista():
        nonlocal lista_abierta
        if lista_abierta:
            partes.append(f"</{lista_abierta}>")
            lista_abierta = None

    for b in bloques:
        tipo = b["type"]
        datos = b.get(tipo, {})
        if tipo in ("bulleted_list_item", "numbered_list_item"):
            etiqueta = "ul" if tipo == "bulleted_list_item" else "ol"
            if lista_abierta != etiqueta:
                cerrar_lista()
                partes.append(f"<{etiqueta}>")
                lista_abierta = etiqueta
            partes.append(f"<li>{a_html(datos.get('rich_text'))}</li>")
            continue
        cerrar_lista()
        if tipo == "paragraph":
            contenido = a_html(datos.get("rich_text"))
            if contenido.strip():
                partes.append(f"<p>{contenido}</p>")
        elif tipo in ("heading_1", "heading_2"):
            partes.append(f"<h2>{a_html(datos.get('rich_text'))}</h2>")
        elif tipo == "heading_3":
            partes.append(f"<h3>{a_html(datos.get('rich_text'))}</h3>")
        elif tipo == "quote":
            partes.append(f"<blockquote>{a_html(datos.get('rich_text'))}</blockquote>")
        elif tipo == "callout":
            partes.append(f'<div class="nota-articulo">{a_html(datos.get("rich_text"))}</div>')
        elif tipo == "divider":
            partes.append("<hr>")
        elif tipo == "image":
            origen = datos.get("file", {}).get("url") or datos.get("external", {}).get("url")
            if origen:
                n_img += 1
                ext = re.search(r"\.(png|jpe?g|gif|webp)", origen.split("?")[0].lower())
                nombre = f"{prefijo_img}-{n_img}.{ext.group(1) if ext else 'jpg'}"
                carpeta_img.mkdir(parents=True, exist_ok=True)
                (carpeta_img / nombre).write_bytes(pedir(origen))
                pie = texto_plano(datos.get("caption"))
                partes.append(f'<figure><img src="img/{nombre}" alt="{html.escape(pie)}" loading="lazy">'
                              + (f"<figcaption>{html.escape(pie)}</figcaption>" if pie else "") + "</figure>")
    cerrar_lista()
    return "\n".join(partes)


def articulos_desde_notion():
    hoy = dt.datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()
    filas = consultar(DS_ARTICULOS, {"or": [
        {"property": "Estado", "status": {"equals": "Aprobado"}},
        {"property": "Estado", "status": {"equals": "Publicado"}},
    ]})
    articulos, vistos = [], set()
    for f in filas:
        programada = valor_fecha(f, "Fecha programada") or f["created_time"][:10]
        if programada > hoy:
            continue  # programado para más adelante
        titulo = valor_texto(f, "Título").strip()
        if not titulo:
            continue
        slug = a_slug(valor_texto(f, "Dirección web").strip() or titulo)
        if slug in vistos:
            raise RuntimeError(f"Dos artículos comparten la dirección «{slug}»")
        vistos.add(slug)
        temas = [o["name"] for o in propiedad(f, "Tema").get("multi_select") or []]
        carpeta = SALIDA / BLOG / slug
        articulos.append({
            "id": f["id"], "estado": (propiedad(f, "Estado").get("status") or {}).get("name"),
            "titulo": titulo, "slug": slug, "temas": temas, "tema": " · ".join(temas),
            # La fecha que se ve en la web es la del día en que se publicó de verdad
            "fecha": valor_fecha(f, "Publicado el") or hoy,
            "resumen": valor_texto(f, "Resumen para Google").strip(),
            "cuerpo": bloques_a_html(hijos(f["id"]), carpeta / "img", slug), "carpeta": carpeta,
        })
    articulos.sort(key=lambda a: a["fecha"], reverse=True)
    return articulos


def articulo_de_prueba():
    """Artículo inventado para revisar el diseño en este Mac. Nunca se publica."""
    hoy = dt.date.today().isoformat()
    cuerpo = ("<p>Este es un artículo de prueba para ver cómo queda el diseño del blog. "
              "<strong>No es un texto real</strong> ni se publica.</p>"
              "<h2>Un subtítulo de ejemplo</h2><p>Aquí iría el contenido, en párrafos cortos y fáciles de leer.</p>"
              "<ul><li>Un punto de una lista.</li><li>Otro punto.</li></ul>"
              "<blockquote>Una frase destacada.</blockquote>"
              '<div class="nota-articulo">Una nota con fondo.</div>')
    return [{"id": None, "estado": "Prueba", "titulo": f"Artículo de prueba {n}", "slug": f"articulo-de-prueba-{n}",
             "temas": [tema], "tema": tema, "fecha": hoy, "resumen": "Resumen de ejemplo para ver la tarjeta y la entradilla.",
             "cuerpo": cuerpo, "carpeta": SALIDA / BLOG / f"articulo-de-prueba-{n}"}
            for n, tema in ((1, "Relaciones"), (2, "Límites"), (3, "Autoestima"))]


def tarjeta_articulo(a, prefijo="", nivel="h3", buscar=None):
    meta = " · ".join(x for x in [fecha_bonita(a["fecha"]), a["tema"]] if x)
    extra = ""
    if buscar is not None:
        extra = (f' data-tema="{html.escape(" ".join(clave_tema(t) for t in a["temas"]))}"'
                 f' data-buscar="{html.escape(buscar)}"')
    return (f'          <a class="articulo-tarjeta" href="{prefijo}{a["slug"]}/"{extra}>\n'
            f'            <span class="etiqueta">{html.escape(meta)}</span>\n'
            f'            <{nivel}>{html.escape(a["titulo"])}</{nivel}>\n'
            + (f'            <p>{html.escape(a["resumen"])}</p>\n' if a["resumen"] else "")
            + '            <span class="articulo-leer">Leer →</span>\n'
            "          </a>")


def sigue_leyendo(a, articulos):
    """Dos artículos más para el final de cada artículo: primero los del mismo tema, luego los más recientes."""
    otros = [o for o in articulos if o["slug"] != a["slug"]]
    otros.sort(key=lambda o: (not set(o["temas"]) & set(a["temas"]),))  # orden estable
    if not otros:
        return ""
    return ('<aside class="articulo-mas">\n          <h2>Sigue leyendo</h2>\n'
            '          <div class="articulos-rejilla">\n'
            + "\n".join(tarjeta_articulo(o, "../") for o in otros[:2])
            + "\n          </div>\n        </aside>")


def pagina_articulo(a, plantilla_blog, articulos):
    """Usa la cabecera y el pie de la página del blog para mantener el diseño."""
    cabeza = plantilla_blog[: plantilla_blog.index("<main")]
    pie = plantilla_blog[plantilla_blog.index("</main>"):]
    titulo = html.escape(a["titulo"])
    descripcion = html.escape(a["resumen"] or a["titulo"])
    url = f"{DOMINIO}/{BLOG}/{a['slug']}/"
    cabeza = re.sub(r"<title>.*?</title>", f"<title>{titulo} · {AUTORA}</title>", cabeza)
    cabeza = re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{descripcion}">', cabeza)
    cabeza = re.sub(r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{url}">', cabeza)
    cabeza = cabeza.replace('<meta property="og:type" content="website">', '<meta property="og:type" content="article">')
    cabeza = re.sub(r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{titulo}">', cabeza)
    cabeza = re.sub(r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{descripcion}">', cabeza)
    cabeza = re.sub(r'<meta property="og:url" content="[^"]*">', f'<meta property="og:url" content="{url}">', cabeza)
    cabeza = cabeza.replace('href="../', 'href="../../').replace('src="../', 'src="../../')
    pie = pie.replace('src="../', 'src="../../').replace('href="../', 'href="../../')
    cabeza = cabeza.replace('<a href="index.html" data-texto="blog.menu">', '<a href="../index.html" data-texto="blog.menu">')
    datos_google = json_para_html({
        "@context": "https://schema.org", "@type": "BlogPosting", "headline": a["titulo"],
        "description": a["resumen"], "datePublished": a["fecha"], "url": url, "inLanguage": "es",
        "author": {"@type": "Person", "@id": f"{DOMINIO}/#sara", "name": AUTORA, "url": f"{DOMINIO}/"},
    })
    cabeza = cabeza.replace("</head>", f'  <script type="application/ld+json">{datos_google}</script>\n</head>')
    meta = " · ".join(x for x in [fecha_bonita(a["fecha"]), a["tema"]] if x)
    cuerpo = f"""<main id="contenido" tabindex="-1">
    <article class="articulo">
      <div class="contenedor articulo-contenedor">
        <a class="articulo-volver" href="../index.html">← <span data-texto="blog.titulo">Comprender para comprenderte</span></a>
        <p class="etiqueta">{html.escape(meta)}</p>
        <h1>{titulo}</h1>
        {f'<p class="entradilla">{descripcion}</p>' if a["resumen"] else ""}
        <div class="articulo-cuerpo">
{a["cuerpo"]}
        </div>
        <p class="articulo-autora"><a href="../../index.html#sobre-mi" data-texto="articulo.autora">Escrito por Sara Molines, psicoterapeuta en Alcoy.</a></p>
        <aside class="articulo-cierre">
          <h2 data-texto="articulo.cierre-titulo">¿Te has visto reflejada?</h2>
          <p data-texto="articulo.cierre-texto">Si algo de lo que has leído te resuena, podemos mirarlo juntas en una primera sesión.</p>
          <a class="boton boton-principal" href="{RESERVAR}" target="_blank" rel="noopener" data-texto="articulo.cierre-boton">Reservar mi primera sesión</a>
        </aside>
        <p class="articulo-aviso" data-texto="articulo.aviso">Este texto es divulgativo y no sustituye a un proceso terapéutico.</p>
        {sigue_leyendo(a, articulos)}
      </div>
    </article>
  """
    return cabeza + cuerpo + pie


def ultimos_articulos(articulos):
    """Los tres últimos artículos, para la portada."""
    return ('    <section class="seccion" id="blog">\n      <div class="contenedor aparece">\n'
            '        <div class="ultimos-titulo"><h2 data-texto="blog.portada-titulo">Para leer con calma</h2>'
            f'<a href="{BLOG}/index.html" data-texto="blog.ver-todos">Ver todos los textos →</a></div>\n'
            '        <div class="articulos-rejilla articulos-rejilla-3">\n'
            + "\n".join(tarjeta_articulo(a, f"{BLOG}/") for a in articulos[:3])
            + "\n        </div>\n      </div>\n    </section>\n    ")


def para_buscar(texto):
    """Minúsculas y sin tildes, para que el buscador encuentre «limites» en «Límites»."""
    sin_tildes = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn").lower()


def clave_tema(tema):
    """«Heridas de infancia» → «heridas-de-infancia»: una palabra por tema, para el filtro del buscador."""
    return re.sub(r"\s+", "-", para_buscar(tema).strip())


def listado_articulos(articulos):
    tarjetas, temas = [], {}
    for a in articulos:
        for t in a["temas"]:
            temas[t] = temas.get(t, 0) + 1
        subtitulos = " ".join(re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h[23]>(.*?)</h[23]>", a["cuerpo"], re.S))
        buscar = para_buscar(html.unescape(" ".join([a["titulo"], a["resumen"], a["tema"], subtitulos])))
        tarjetas.append(tarjeta_articulo(a, nivel="h2", buscar=buscar))
    filtros = [f'<button type="button" class="filtro activo" data-tema="" aria-pressed="true">Todos <span>{len(articulos)}</span></button>']
    filtros += [f'<button type="button" class="filtro" data-tema="{html.escape(clave_tema(t))}" aria-pressed="false">'
                f'{html.escape(t)} <span>{n}</span></button>' for t, n in sorted(temas.items())]
    buscador = (
        '        <div class="buscador" data-buscador>\n'
        '          <label class="buscador-campo">\n'
        '            <span class="etiqueta">Buscar</span>\n'
        '            <input type="search" placeholder="límites, culpa, ansiedad…" autocomplete="off" spellcheck="false">\n'
        '          </label>\n'
        '          <div class="buscador-temas" role="group" aria-label="Filtrar por tema">\n            '
        + "\n            ".join(filtros) + "\n"
        '          </div>\n'
        '          <p class="buscador-cuenta" aria-live="polite"></p>\n'
        '        </div>\n'
    )
    vacio = (
        '        <div class="buscador-vacio" hidden>\n'
        '          <p>Todavía no he escrito sobre eso. Prueba con otra palabra o con otro tema.</p>\n'
        '          <button type="button" class="boton boton-secundario" data-limpiar>Ver todos los textos</button>\n'
        '        </div>\n'
    )
    return ('    <section class="blog-listado">\n      <div class="contenedor">\n' + buscador
            + '        <div class="articulos-rejilla">\n' + "\n".join(tarjetas) + "\n        </div>\n" + vacio
            + "      </div>\n    </section>\n    ")


def marcar_publicados(articulos):
    for a in articulos:
        if a["estado"] == "Aprobado":
            notion(f"pages/{a['id']}", "PATCH", {"properties": {
                "Estado": {"status": {"name": "Publicado"}},
                "Publicado el": {"date": {"start": a["fecha"]}},
            }})


# ---------------------------------------------------------------- archivos para buscadores

def escribir_sitemap(articulos):
    hoy = dt.date.today().isoformat()
    urls = [(f"{DOMINIO}/", hoy), (f"{DOMINIO}/{BLOG}/", hoy), (f"{DOMINIO}/aviso-legal/", None)]
    urls += [(f"{DOMINIO}/{BLOG}/{a['slug']}/", a["fecha"]) for a in articulos]
    filas = "".join(f"  <url>\n    <loc>{u}</loc>\n" + (f"    <lastmod>{f}</lastmod>\n" if f else "") + "  </url>\n" for u, f in urls)
    escribir(SALIDA / "sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?>\n'
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + filas + "</urlset>\n")


def escribir_feed(articulos, textos):
    """Feed RSS del blog (blog/feed.xml), con el artículo completo."""
    def cdata(texto):
        return "<![CDATA[" + texto.replace("]]>", "]]]]><![CDATA[>") + "]]>"
    nombre = sin_marcas(textos.get("blog.titulo", "Blog"))
    items = []
    for a in articulos[:20]:
        url = f"{DOMINIO}/{BLOG}/{a['slug']}/"
        cuerpo = a["cuerpo"].replace('src="img/', f'src="{url}img/')
        fecha = dt.datetime.fromisoformat(a["fecha"]).replace(hour=8, tzinfo=ZoneInfo("Europe/Madrid"))
        items.append(f"    <item>\n      <title>{html.escape(a['titulo'])}</title>\n      <link>{url}</link>\n"
                     f"      <guid isPermaLink=\"true\">{url}</guid>\n      <pubDate>{format_datetime(fecha)}</pubDate>\n"
                     f"      <description>{cdata(a['resumen'])}</description>\n"
                     f"      <content:encoded>{cdata(cuerpo)}</content:encoded>\n    </item>\n")
    escribir(SALIDA / BLOG / "feed.xml",
             '<?xml version="1.0" encoding="UTF-8"?>\n'
             '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">\n'
             f"  <channel>\n    <title>{html.escape(nombre)} · {AUTORA}</title>\n    <link>{DOMINIO}/{BLOG}/</link>\n"
             f"    <description>{html.escape(sin_marcas(textos.get('blog.intro', '')))}</description>\n    <language>es</language>\n"
             f'    <atom:link href="{DOMINIO}/{BLOG}/feed.xml" rel="self" type="application/rss+xml"/>\n'
             + "".join(items) + "  </channel>\n</rss>\n")


def escribir_llms(articulos, textos):
    """llms.txt: resumen de la web para los asistentes de IA."""
    lineas = [f"# {AUTORA}", "",
              "> " + sin_marcas(textos.get("portada.titulo", "")) + ". " + sin_marcas(textos.get("portada.modalidad", "")) + ".", "",
              "## Páginas", "",
              f"- [Inicio]({DOMINIO}/): quién es Sara Molines, cómo trabaja y cómo reservar una primera sesión.",
              f"- [{sin_marcas(textos.get('blog.titulo', 'Blog'))}]({DOMINIO}/{BLOG}/): {sin_marcas(textos.get('blog.intro', ''))}",
              f"- [Aviso legal y privacidad]({DOMINIO}/aviso-legal/)", ""]
    if articulos:
        lineas += ["## Artículos", ""]
        lineas += [f"- [{a['titulo']}]({DOMINIO}/{BLOG}/{a['slug']}/)" + (f": {a['resumen']}" if a["resumen"] else "") for a in articulos]
        lineas.append("")
    escribir(SALIDA / "llms.txt", "\n".join(lineas))


def pagina_404(plantilla_blog):
    """Página de error con el diseño de la web. GitHub Pages la sirve en cualquier dirección que no exista."""
    cabeza = plantilla_blog[: plantilla_blog.index("<main")]
    pie = plantilla_blog[plantilla_blog.index("</main>"):]
    cabeza = re.sub(r"<title>.*?</title>", f"<title>Página no encontrada · {AUTORA}</title>", cabeza)
    cabeza = re.sub(r'\s*<link rel="canonical" href="[^"]*">', '\n  <meta name="robots" content="noindex">', cabeza)
    cabeza = re.sub(r'\s*<meta (property="og:|name="twitter:)[^>]*>', "", cabeza)
    cabeza = cabeza.replace('href="index.html"', f'href="/{BLOG}/"')
    cabeza = cabeza.replace('href="../', 'href="/').replace('src="../', 'src="/').replace('href="/index.html', 'href="/')
    pie = pie.replace('src="../', 'src="/').replace('href="../', 'href="/').replace('href="/aviso-legal/index.html"', 'href="/aviso-legal/"')
    cuerpo = f"""<main id="contenido" tabindex="-1">
    <section class="blog-vacio">
      <div class="contenedor">
        <span class="sello">Error 404</span>
        <h1 style="margin-bottom: 18px;">Esta página no existe</h1>
        <p>Puede que la dirección esté mal escrita o que haya cambiado de sitio. Desde aquí puedes volver al inicio.</p>
        <div class="botones botones-centro">
          <a href="/" class="boton boton-principal">Ir al inicio</a>
          <a href="/{BLOG}/" class="boton boton-secundario">Ver el blog</a>
        </div>
      </div>
    </section>
  """
    return cabeza + cuerpo + pie


def versionar_archivos():
    """Añade ?v=… a estilos.css, script.js y textos.js en todas las páginas, para que
    el navegador no use una copia antigua cuando cambian."""
    version = {n: hashlib.md5((SALIDA / n).read_bytes()).hexdigest()[:8]
               for n in ("estilos.css", "script.js", "textos.js")}
    for pagina in SALIDA.rglob("*.html"):
        texto = leer(pagina)
        nuevo = re.sub(r'(estilos\.css|script\.js|textos\.js)"', lambda m: f'{m.group(1)}?v={version[m.group(1)]}"', texto)
        if nuevo != texto:
            escribir(pagina, nuevo)


def enlaces_limpios():
    """Cambia los enlaces internos a «…/index.html» por su dirección limpia («…/»), que es la
    canónica. En la carpeta local se mantienen con index.html para poder abrirlas con doble clic."""
    patron = re.compile(r'href="((?:\.\./)*(?:[\w-]+/)*)index\.html(#[^"]*)?"')
    for pagina in SALIDA.rglob("*.html"):
        texto = leer(pagina)
        nuevo = patron.sub(lambda m: f'href="{m.group(1) or "./"}{m.group(2) or ""}"', texto)
        if nuevo != texto:
            escribir(pagina, nuevo)


# ---------------------------------------------------------------- principal

def paso(nombre, funcion):
    try:
        resultado = funcion()
        estado[nombre] = "ok"
        return resultado
    except Exception as e:
        estado[nombre] = f"error: {e}"
        print(f"[{nombre}] {e}", file=sys.stderr)
        if NOTION_TOKEN:
            traceback.print_exc()
        return None


def comprobar_fuentes():
    """Si Notion ha fallado, no se publica: la web se queda como estaba hasta la siguiente ejecución.
    En este Mac, sin la clave de Notion, no se aplica: se genera la vista previa con lo que hay."""
    fallos = {k: v for k, v in estado.items() if k.endswith("_notion") and str(v).startswith("error")}
    if fallos and NOTION_TOKEN:
        print(json.dumps(estado, ensure_ascii=False, indent=2))
        sys.exit("No se publica: Notion ha fallado en " + ", ".join(fallos) + ". La web se queda como estaba.")


def comprobar_pendientes():
    """Ninguna página puede salir a internet con datos sin confirmar (class="pendiente")."""
    con_pendientes = sorted(str(p.relative_to(SALIDA)) for p in SALIDA.rglob("*.html") if 'class="pendiente"' in leer(p))
    estado["pendientes"] = con_pendientes
    if con_pendientes and EN_GITHUB:
        print(json.dumps(estado, ensure_ascii=False, indent=2))
        sys.exit("No se publica: quedan datos pendientes en " + ", ".join(con_pendientes))


def main():
    if SALIDA.exists():
        shutil.rmtree(SALIDA)
    shutil.copytree(RAIZ, SALIDA, ignore=shutil.ignore_patterns(".*", "_site", "robot", "__pycache__"))

    portada = leer(SALIDA / "index.html")
    blog = leer(SALIDA / BLOG / "index.html")

    # 1. Textos
    textos = leer_textos_js()
    de_notion = paso("textos_notion", textos_desde_notion)
    if de_notion:
        textos.update(de_notion)
        estado["textos"] = len(de_notion)
    portada = reemplazar_entre(portada, "FAQ-GOOGLE", faq_para_google(portada, textos))

    # 2. Testimonios
    de_notion = paso("testimonios_notion", testimonios_desde_notion)
    opiniones = de_notion or []
    if PRUEBA and not EN_GITHUB:
        opiniones = testimonios_de_prueba() + opiniones
    # Si Notion ha respondido, manda Notion: sin opiniones visibles, el bloque desaparece.
    # Sin Notion (en este Mac) se queda la copia local que trae index.html.
    # Desde el 14/11/2026 los testimonios son tarjetas fijas con capturas de WhatsApp: si la portada ya no
    # tiene las marcas del robot, no se toca.
    if "<!-- ROBOT:TESTIMONIOS:INICIO -->" in portada and (opiniones or de_notion is not None):
        portada = reemplazar_entre(portada, "TESTIMONIOS", seccion_testimonios(opiniones) if opiniones else "")
    estado["testimonios"] = len(opiniones)

    # 3. Artículos
    articulos = paso("articulos_notion", articulos_desde_notion) or []
    if PRUEBA and not EN_GITHUB:
        articulos = articulo_de_prueba() + articulos
    for a in articulos:
        escribir(a["carpeta"] / "index.html", volcar_textos(pagina_articulo(a, blog, articulos), textos))
    if articulos:
        blog = reemplazar_entre(blog, "ARTICULOS", listado_articulos(articulos))
        portada = reemplazar_entre(portada, "ULTIMOS", ultimos_articulos(articulos))
        cabecera_feed = (f'  <link rel="alternate" type="application/rss+xml" title="{AUTORA}" '
                         f'href="{DOMINIO}/{BLOG}/feed.xml">\n</head>')
        portada, blog = portada.replace("</head>", cabecera_feed), blog.replace("</head>", cabecera_feed)
        escribir_feed(articulos, textos)
    estado["articulos"] = [a["slug"] for a in articulos]

    escribir_textos_js(textos)
    escribir(SALIDA / "index.html", volcar_textos(portada, textos))
    escribir(SALIDA / BLOG / "index.html", volcar_textos(blog, textos))
    legal = SALIDA / "aviso-legal" / "index.html"
    escribir(legal, volcar_textos(leer(legal), textos))
    escribir(SALIDA / "404.html", volcar_textos(pagina_404(blog), textos))
    escribir_sitemap(articulos)
    escribir_llms(articulos, textos)

    comprobar_fuentes()
    comprobar_pendientes()
    if articulos and EN_GITHUB:
        paso("marcar_publicados", lambda: marcar_publicados(articulos))

    enlaces_limpios()
    versionar_archivos()
    escribir(SALIDA / "robot-estado.json", json.dumps(estado, ensure_ascii=False, indent=2))
    print(json.dumps(estado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

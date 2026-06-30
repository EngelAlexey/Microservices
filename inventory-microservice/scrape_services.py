import requests
import logging
import re
import html as _html
from urllib.parse import urljoin, urlparse

from render_services import is_configured as cf_configured, render_content

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-419,es;q=0.8,en-US;q=0.5,en;q=0.3",
}

# Pistas de que una URL de imagen es el logo/placeholder del sitio, no la foto del producto.
# Evita el bug histórico de guardar el `og:image` (que en varias tiendas es el logo).
_NON_PRODUCT_IMG = ("logo", "placeholder", "sprite", "favicon", "icon-", "/icons/", "no-image", "noimage")


def _looks_like_product_image(u: str | None) -> bool:
    if not u:
        return False
    return not any(h in u.lower() for h in _NON_PRODUCT_IMG)


def _ellagar_image(url: str) -> str | None:
    """El Lagar sirve la imagen del producto de forma determinística por código:
    `.../Articulos_MED/{codigo}_MED.png` (alta) — el código va en la URL `/DetalleArticulo/{codigo}/…`.
    Preferimos MED (alta) pero NO todos los artículos la tienen; si no existe (404) devolvemos None
    para que la cascada use el `og:image` (versión PEQ) que sí entrega el HTML renderizado."""
    m = re.search(r"/DetalleArticulo/(\d+)", url or "")
    if not m:
        return None
    med = f"https://www.ellagar.com/SERV_ADMIN_FILES/Archivos/Imagenes/Articulos_MED/{m.group(1)}_MED.png"
    try:
        if requests.head(med, headers=_HEADERS, timeout=8).status_code == 200:
            return med
    except Exception:
        pass
    return None


# Reglas de imagen por dominio: cuando conocemos el patrón determinístico de imagen de un
# proveedor (mejor/más confiable que rasguñar el HTML), se aplica primero. Extensible.
_DOMAIN_IMAGE_RULES = {"ellagar.com": _ellagar_image}


def extract_product_image(html: str, url: str | None = None) -> str | None:
    """Extrae la URL de la IMAGEN del producto de un HTML (idealmente ya renderizado).

    1) Regla por dominio si existe (imagen determinística de alta resolución, p.ej. El Lagar MED).
    2) Cascada genérica descartando logos: og:image → <img> → CSS background-url (cubre el
       `div.lupa` con `background:url(...)`). Devuelve None si solo aparecen logos/placeholders."""
    if url:
        host = urlparse(url).netloc.lower()
        host = host[4:] if host.startswith("www.") else host
        rule = _DOMAIN_IMAGE_RULES.get(host)
        if rule:
            u = rule(url)
            if u:
                return u
    if not html:
        return None
    candidates = []

    # 1) og:image (ambos órdenes de atributos)
    for pat in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)["\']',
        r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:image["\']',
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            candidates.append(m.group(1))

    # 2) <img src> / data-src de producto
    for m in re.finditer(r'<img[^>]+(?:src|data-src)=["\'](https?://[^"\']+?\.(?:jpg|jpeg|png|webp))', html, re.IGNORECASE):
        candidates.append(m.group(1))

    # 3) CSS background-image: url(...) — el `div.lupa` y similares (comillas pueden venir como &quot;)
    h = _html.unescape(html)
    for m in re.finditer(r'background(?:-image)?\s*:\s*url\(\s*["\']?(https?://[^)"\']+?\.(?:jpg|jpeg|png|webp))', h, re.IGNORECASE):
        candidates.append(m.group(1))

    for u in candidates:
        if _looks_like_product_image(u):
            return u
    return None


def scrape_product_page(url: str) -> tuple[str, str | None]:
    """Devuelve (html, image_url). Usa Cloudflare Browser Rendering (render JS) cuando está
    configurado —necesario para SPAs/catálogos JS—; si no, o si falla, cae a `requests`."""
    html = None
    if cf_configured():
        html = render_content(url)
        if not html:
            logger.info(f"Cloudflare no devolvió HTML para '{url}'; se intenta requests")
    if not html:
        try:
            response = requests.get(url, headers=_HEADERS, timeout=15)
            response.raise_for_status()
            html = response.text
        except Exception as e:
            logger.warning(f"No se pudo scrapear la página '{url}': {e}")
            return None, None
    return html, extract_product_image(html, url)

def extract_relevant_content(html: str, max_chars: int = 40000) -> str:
    """Reduce el HTML crudo al contenido útil del producto antes de mandarlo a la IA.

    El HTML completo está dominado por <head>, scripts, CSS y navegación del sitio,
    donde vive la meta-description genérica de la tienda. Esto prioriza los datos
    estructurados del producto (JSON-LD schema.org) y el texto real del <body>,
    de modo que la descripción específica del producto entre dentro del límite.
    """
    if not html:
        return ""

    parts = []

    # 1) Datos estructurados JSON-LD (schema.org/Product trae 'description' real del producto)
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL,
    ):
        block = m.group(1).strip()
        if block:
            parts.append("[STRUCTURED DATA JSON-LD]\n" + block)

    # 2) Texto del <body> sin ruido (scripts, estilos, navegación, etc.)
    body_match = re.search(r'<body[^>]*>(.*?)</body>', html, re.IGNORECASE | re.DOTALL)
    body = body_match.group(1) if body_match else html
    body = re.sub(r'<!--.*?-->', ' ', body, flags=re.DOTALL)
    body = re.sub(
        r'<(script|style|svg|noscript|nav|header|footer|form|iframe)\b[^>]*>.*?</\1>',
        ' ', body, flags=re.IGNORECASE | re.DOTALL,
    )
    body_text = re.sub(r'<[^>]+>', ' ', body)
    body_text = _html.unescape(body_text)
    body_text = re.sub(r'\s+', ' ', body_text).strip()
    if body_text:
        parts.append("[PAGE TEXT]\n" + body_text)

    return "\n\n".join(parts)[:max_chars]


def download_image_from_url(image_url: str) -> tuple[bytes | None, str | None]:
    if not image_url:
        return None, None
    try:
        response = requests.get(image_url, headers=_HEADERS, timeout=15)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "image/jpeg")
        return response.content, content_type
    except Exception as e:
        logger.warning(f"No se pudo descargar la imagen '{image_url}': {e}")
        return None, None

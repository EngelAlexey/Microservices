import requests
import logging
import re
import html as _html
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-419,es;q=0.8,en-US;q=0.5,en;q=0.3",
}

def scrape_product_page(url: str) -> tuple[str, str | None]:
    try:
        response = requests.get(url, headers=_HEADERS, timeout=15)
        response.raise_for_status()
        html = response.text
        og_match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\'](https?://[^"\']+)["\']', html, re.IGNORECASE)
        if not og_match:
            og_match = re.search(r'<meta[^>]+content=["\'](https?://[^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.IGNORECASE)
        image_url = og_match.group(1) if og_match else None
        if not image_url:
            img_match = re.search(r'<img[^>]+src=["\'](https?://[^"\']+\.(jpg|jpeg|png|webp))["\']', html, re.IGNORECASE)
            if img_match:
                image_url = img_match.group(1)
        return html, image_url
    except Exception as e:
        logger.warning(f"No se pudo scrapear la página '{url}': {e}")
        return None, None

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

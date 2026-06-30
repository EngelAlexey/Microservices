"""Resolvers de producto por proveedor / plataforma — capa de estandarización del scraping.

El objetivo es resolver el producto de **cualquier** página de proveedor con una cascada
estandarizada (de lo más preciso/barato a lo más genérico):

  1. **Resolver dedicado por dominio** — para sitios con una fuente de datos propia mejor que
     scrapear (p.ej. Construplaza expone su catálogo en Algolia; su HTML no rinde ni con render).
  2. **Resolver de plataforma** — muchas tiendas comparten plataforma de e-commerce. Un solo
     resolver cubre decenas de sitios. Hoy: **Shopify** (`<url>.json` da el producto completo).
  3. **Camino genérico** (en el endpoint, no aquí): Cloudflare render + extracción con IA. Cubre
     El Lagar y la mayoría de SPAs "normales". Las imágenes determinísticas por dominio (p.ej.
     El Lagar MED) se aplican en `scrape_services.extract_product_image`.

`resolve_supplier(url)` recorre (1) y (2) y devuelve un dict con el esquema de
`ai_services.extract_product_from_html` (itTitle, itBrand, itCategory, ...) + `image_url`,
`barcode`, `cabys`, `itWebsite`; o `None` si nadie resolvió (el caller cae al camino genérico).
"""
import os
import re
import json
import logging
from urllib.parse import urlparse, unquote

import requests

from ai_services import extract_product_from_html

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}


def _host(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _structure_with_ai(desc: str, marca: str = "", categoria: str = "", unidad: str = "") -> dict:
    """Resuelve la jerarquía (itTitle genérico vs itModel/itSize, marca, categoría, unidad) con
    Gemini a partir de datos LIMPIOS (no HTML ruidoso), reusando su prompt afinado. Fallback a los
    campos crudos si la IA no devuelve título."""
    clean = (
        f"Nombre del producto: {desc}\n"
        f"Marca: {marca}\n"
        f"Categoría: {categoria}\n"
        f"Unidad de venta: {unidad}"
    )
    data = extract_product_from_html(clean) or {}
    if not str(data.get("itTitle") or "").strip():
        data = {"itTitle": (desc or "").strip(), "itBrand": marca, "itUnit": unidad}
    if categoria and not data.get("itCategory"):
        data["itCategory"] = categoria.split(">")[0].strip()
    return data


# ============================ Resolvers dedicados por dominio ============================

# --- Construplaza: catálogo en Algolia (índice "Products") --------------------------------------
# Credenciales search-only (públicas/client-side) del bundle /bundles/RequeriredUtils. La imagen
# real vive en el CDN cloudfront; el og:image del sitio es solo el logo. Override por env si rotan.
_CP_APP = os.getenv("CP_ALGOLIA_APP_ID", "MUCJNSQCZH")
_CP_KEY = os.getenv("CP_ALGOLIA_KEY", "548b5dedba445fcdb9435d2dd720562a")
_CP_INDEX = os.getenv("CP_ALGOLIA_INDEX", "Products")
_CP_IMG = "https://dpbfouxy1lg2q.cloudfront.net/{art}/{art}-1.webp"  # -1 = full; -thumbnail = mini


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower().replace("/", " "))).strip()


def _algolia_query(app: str, key: str, index: str, query: str, hits: int = 10) -> list:
    url = f"https://{app}-dsn.algolia.net/1/indexes/{index}/query"
    headers = {"X-Algolia-Application-Id": app, "X-Algolia-API-Key": key, "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, data=json.dumps({"params": f"query={query}&hitsPerPage={hits}"}), timeout=20)
    resp.raise_for_status()
    return resp.json().get("hits", [])


def _resolve_construplaza(url: str) -> dict | None:
    slug = unquote(urlparse(url).path.rstrip("/").split("/")[-1]).replace("-", " ")
    if not slug:
        return None
    hits = _algolia_query(_CP_APP, _CP_KEY, _CP_INDEX, slug)
    if not hits:
        logger.info(f"Construplaza/Algolia sin resultados para '{slug}'")
        return None
    # Mejor coincidencia por solapamiento de tokens del slug con la Descripción ("3/8" llega como "38").
    target = set(_norm(re.sub(r"\b(\d)8\b", r"\1 8", slug)).split())
    best = max(hits, key=lambda h: len(target & set(_norm(h.get("Descripcion")).split())))
    articulo = str(best.get("Articulo") or best.get("objectID") or "").strip()

    data = _structure_with_ai(
        str(best.get("Descripcion") or "").strip(),
        str(best.get("Marca") or "").strip(),
        str(best.get("Categoria") or "").strip(),
        str(best.get("Unidad") or "").strip(),
    )
    data["itWebsite"] = url
    data["image_url"] = _CP_IMG.format(art=articulo) if articulo else best.get("Image")
    data["barcode"] = str(best.get("CodBarras") or "").strip() or None
    data["cabys"] = str(best.get("Cabys") or "").strip()
    logger.info(f"Construplaza/Algolia resolvió '{slug}' -> Articulo {articulo}")
    return data


# Registro dominio -> resolver dedicado. Agregar aquí otros proveedores con fuente propia.
_RESOLVERS = {
    "construplaza.com": _resolve_construplaza,
}


# ============================ Resolvers de plataforma ============================

def _resolve_shopify(url: str) -> dict | None:
    """Shopify expone el producto en `<product-url>.json`. Cubre decenas de tiendas con un solo
    resolver. Devuelve None rápido (sin IA) si la URL no es un producto Shopify."""
    if "/products/" not in url:  # convención de URL de producto Shopify (evita requests en vano)
        return None
    base = url.split("?")[0].split("#")[0].rstrip("/")
    try:
        r = requests.get(base + ".json", headers=_HEADERS, timeout=15)
        if r.status_code != 200 or "json" not in (r.headers.get("Content-Type") or "").lower():
            return None
        product = (r.json() or {}).get("product")
    except Exception:
        return None
    if not product or not str(product.get("title") or "").strip():
        return None

    images = product.get("images") or []
    variants = product.get("variants") or []
    data = _structure_with_ai(
        str(product.get("title") or "").strip(),
        str(product.get("vendor") or "").strip(),
        str(product.get("product_type") or "").strip(),
    )
    if not data.get("itDescription"):
        data["itDescription"] = _strip_html(product.get("body_html"))[:500]
    barcode = next((v.get("barcode") for v in variants if v.get("barcode")), None)
    data["itWebsite"] = url
    data["image_url"] = (images[0].get("src") if images else None)
    data["barcode"] = str(barcode).strip() if barcode else None
    data["cabys"] = ""
    logger.info(f"Shopify resolvió '{base}' -> {product.get('title')}")
    return data


def _resolve_vtex(url: str) -> dict | None:
    """VTEX expone el catálogo en `/api/catalog_system/pub/products/search/{slug}/p`. Las URLs de
    producto VTEX terminan en `/p` (gate para no pegarle a tiendas no-VTEX). Cubre muchas tiendas."""
    parts = urlparse(url)
    path = parts.path.rstrip("/")
    if not path.endswith("/p"):
        return None
    slug = path[:-2].rstrip("/").split("/")[-1]
    if not slug:
        return None
    origin = f"{parts.scheme}://{parts.netloc}"
    try:
        r = requests.get(f"{origin}/api/catalog_system/pub/products/search/{slug}/p", headers=_HEADERS, timeout=15)
        if r.status_code != 200 or "json" not in (r.headers.get("Content-Type") or "").lower():
            return None
        arr = r.json()
    except Exception:
        return None
    if not isinstance(arr, list) or not arr or not str((arr[0] or {}).get("productName") or "").strip():
        return None

    p = arr[0]
    item0 = (p.get("items") or [{}])[0] or {}
    images = item0.get("images") or []
    cats = p.get("categories") or []
    categoria = cats[0].strip("/").replace("/", " > ") if cats else ""
    data = _structure_with_ai(
        str(p.get("productName") or "").strip(),
        str(p.get("brand") or "").strip(),
        categoria,
        str(item0.get("measurementUnit") or "").strip(),
    )
    if not data.get("itDescription"):
        data["itDescription"] = _strip_html(p.get("description"))[:500]
    data["itWebsite"] = url
    data["image_url"] = images[0].get("imageUrl") if images else None
    data["barcode"] = str(item0.get("ean") or "").strip() or None
    data["cabys"] = ""
    logger.info(f"VTEX resolvió '{slug}' -> {p.get('productName')}")
    return data


# Resolvers de plataforma (se prueban con cualquier dominio si no hubo resolver dedicado).
_PLATFORM_RESOLVERS = [_resolve_shopify, _resolve_vtex]


# ============================ Extracción JSON-LD (sobre HTML renderizado) ============================

def _iter_jsonld(obj):
    """Itera nodos de un JSON-LD (maneja listas y `@graph`)."""
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_jsonld(x)
    elif isinstance(obj, dict):
        for x in (obj.get("@graph") or []):
            yield from _iter_jsonld(x)
        yield obj


def _jsonld_product(html: str) -> dict | None:
    if not html:
        return None
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            obj = json.loads(m.group(1).strip())
        except Exception:
            continue
        for node in _iter_jsonld(obj):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(str(x).lower() == "product" for x in types) and node.get("name"):
                return node
    return None


def resolve_from_html(html: str, url: str) -> dict | None:
    """Resolver GENÉRICO basado en datos estructurados **schema.org/Product (JSON-LD)** del HTML
    ya renderizado: trae imagen y GTIN (código de barras) de forma confiable, presentes en muchas
    tiendas. Devuelve data + image_url + barcode, o None si la página no trae Product JSON-LD (el
    caller cae a la extracción con IA sobre el HTML). Se usa en el camino genérico, tras el render."""
    node = _jsonld_product(html)
    if not node:
        return None
    brand = node.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    img = node.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url")
    gtin = node.get("gtin13") or node.get("gtin14") or node.get("gtin12") or node.get("gtin")

    data = _structure_with_ai(str(node.get("name")), str(brand or ""), str(node.get("category") or ""), "")
    if not data.get("itDescription"):
        data["itDescription"] = _strip_html(node.get("description"))[:500]
    data["itWebsite"] = url
    if img and "logo" not in str(img).lower():
        data["image_url"] = str(img)
    data["barcode"] = str(gtin).strip() if gtin else None
    data["cabys"] = ""
    logger.info(f"JSON-LD Product resolvió '{url}' -> {node.get('name')}")
    return data


def resolve_supplier(url: str) -> dict | None:
    """Cascada: resolver dedicado del dominio → resolvers de plataforma (Shopify, ...). Devuelve
    el primer resultado o None (el caller cae al scraping genérico con Cloudflare render + IA)."""
    fn = _RESOLVERS.get(_host(url))
    if fn:
        try:
            data = fn(url)
            if data:
                return data
        except Exception as e:
            logger.warning(f"Resolver dedicado falló para '{url}': {e}")
    for resolver in _PLATFORM_RESOLVERS:
        try:
            data = resolver(url)
            if data:
                return data
        except Exception as e:
            logger.warning(f"Resolver de plataforma {resolver.__name__} falló para '{url}': {e}")
    return None

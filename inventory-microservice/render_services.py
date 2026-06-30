"""Cliente de Cloudflare Browser Rendering (REST API).

Renderiza páginas con un navegador headless gestionado por Cloudflare, de modo que el
contenido cargado por JavaScript (SPAs como Construplaza/El Lagar, catálogos Algolia, etc.)
sí está disponible. Pensado como motor de scraping **genérico** —sirve para cualquier sitio,
sin parsers a medida— y **reutilizable**: además del `render_content` (HTML, que alimenta la
extracción de productos), expone `render_markdown` (texto limpio, ideal para que otros
servicios —p.ej. Kaizen AI— lean/“busquen” en la web y se lo pasen a un LLM).

Cuota (plan Free): 10 min de navegador/día, 3 concurrentes, 1 instancia nueva cada 20 s.
Suficiente para pruebas; para producción se evalúa el plan Workers Paid (10 h/mes incluidas).

Variables de entorno:
- CF_ACCOUNT_ID   — Account ID de Cloudflare.
- CF_API_TOKEN    — API Token con permiso "Browser Rendering – Edit".
- CF_RENDER_TIMEOUT (opcional) — timeout HTTP en segundos (por defecto 60).
"""
import os
import time
import logging

import requests

logger = logging.getLogger(__name__)

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "")
_TIMEOUT_S = int(os.getenv("CF_RENDER_TIMEOUT", "60"))
_RETRY_DELAYS = [1.0, 3.0]  # reintenta solo ante errores transitorios (5xx/429/red)

_BASE = "https://api.cloudflare.com/client/v4/accounts/{acct}/browser-rendering/{endpoint}"


class RenderError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body or ""
        super().__init__(f"Cloudflare Render error {status}: {self.body[:300]}")


def is_configured() -> bool:
    """True si hay credenciales de Cloudflare Browser Rendering en el entorno."""
    return bool(CF_ACCOUNT_ID and CF_API_TOKEN)


def _is_transient(status: int) -> bool:
    return status >= 500 or status == 429


def _post(endpoint: str, body: dict):
    """POST a un endpoint de Browser Rendering. Devuelve el campo `result` de la respuesta
    (string para content/markdown/screenshot-b64, lista/obj para scrape/json). Lanza RenderError."""
    if not is_configured():
        raise RenderError(0, "Cloudflare no configurado (falta CF_ACCOUNT_ID/CF_API_TOKEN)")
    url = _BASE.format(acct=CF_ACCOUNT_ID, endpoint=endpoint)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {CF_API_TOKEN}"}

    last_err = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=_TIMEOUT_S)
        except requests.RequestException as e:
            last_err = RenderError(0, f"error de red: {e}")
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt]); continue
            raise last_err

        if not resp.ok:
            err = RenderError(resp.status_code, resp.text)
            if _is_transient(resp.status_code) and attempt < len(_RETRY_DELAYS):
                last_err = err
                time.sleep(_RETRY_DELAYS[attempt]); continue
            raise err

        try:
            data = resp.json()
        except ValueError:
            raise RenderError(resp.status_code, f"respuesta no-JSON: {resp.text[:200]}")
        # Envoltura estándar de la API v4: {success, result, errors, messages}
        if not data.get("success", False):
            raise RenderError(resp.status_code, str(data.get("errors") or data))
        return data.get("result")

    raise last_err or RenderError(0, "Cloudflare inalcanzable")


def render_content(url: str, wait_until: str = "networkidle0", reject_resource_types=None,
                   wait_for_timeout: int | None = None, wait_for_selector: str | None = None) -> str | None:
    """Devuelve el HTML ya renderizado (post-JS) de `url`, o None si falla.

    `reject_resource_types` (lista, p.ej. ["image","font","media"]) acelera/abarata el render
    bloqueando recursos no necesarios para extraer datos. None = cargar todo (necesario si luego
    se quiere la URL de la imagen del producto).
    `wait_for_timeout` (ms) espera extra tras cargar — útil para sitios que pintan el contenido
    vía XHR/Algolia después del `networkidle0`. `wait_for_selector` (CSS) espera a que aparezca
    un elemento concreto (más preciso que un timeout fijo)."""
    body = {"url": url, "gotoOptions": {"waitUntil": wait_until}}
    if reject_resource_types:
        body["rejectResourceTypes"] = reject_resource_types
    if wait_for_timeout:
        body["waitForTimeout"] = wait_for_timeout
    if wait_for_selector:
        body["waitForSelector"] = {"selector": wait_for_selector}
    try:
        html = _post("content", body)
        return html if isinstance(html, str) and html.strip() else None
    except RenderError as e:
        logger.warning(f"render_content falló para '{url}': {e}")
        return None


def render_markdown(url: str, wait_until: str = "networkidle0") -> str | None:
    """Devuelve el contenido de `url` como Markdown limpio (post-JS), o None si falla.

    Reutilizable por otros servicios (p.ej. Kaizen AI) que necesiten leer/buscar en la web y
    pasar texto legible a un LLM, sin el ruido de HTML/JS/CSS."""
    try:
        md = _post("markdown", {"url": url, "gotoOptions": {"waitUntil": wait_until}})
        return md if isinstance(md, str) and md.strip() else None
    except RenderError as e:
        logger.warning(f"render_markdown falló para '{url}': {e}")
        return None

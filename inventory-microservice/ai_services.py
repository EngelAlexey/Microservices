import os
import json
import logging
from dotenv import load_dotenv

load_dotenv()

from google import genai
from google.genai import types
from scrape_services import extract_relevant_content

logger = logging.getLogger(__name__)

# genai.Client recibe la api_key explícita; no debe depender de GOOGLE_API_KEY del entorno
# (esa variable la usa drive_services para el fallback de descarga pública de Drive).
_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

_GEMINI_MODEL = "gemini-2.5-flash"


def _json_config(**overrides):
    """Config común para las llamadas a Gemini que devuelven JSON."""
    params = dict(
        response_mime_type="application/json",
        temperature=0.1,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    params.update(overrides)
    return types.GenerateContentConfig(**params)


def _parse_response_json(response):
    """Limpia las fences ```json y parsea el JSON de la respuesta del modelo."""
    text = response.text.replace('```json', '').replace('```', '')
    return json.loads(text)


def _attach_usage(response, data):
    """Adjunta el conteo de tokens a la data si el modelo lo reportó."""
    usage = response.usage_metadata
    if usage:
        data['usage'] = {
            'prompt_tokens': usage.prompt_token_count,
            'candidates_tokens': usage.candidates_token_count,
            'total_tokens': usage.total_token_count
        }
    return data

_PROMPT = """Extract data from this Costa Rican invoice PDF. Be extremely precise with financial totals and taxes.

Rules for Financial Data:
1. CurrencyID: Strictly identify if it is USD ($) or CRC (₡). Do not assume. Check the symbols and text (e.g., 'US DOLLAR', '$').
2. doAccount: Identify the account type. 
   - If it is a "Factura Electrónica" or "Tiquete", set to "CXC" (Cuenta por Cobrar).
   - If it is a "Nota de Crédito" or "Factura de Compra", set to "CXP" (Cuenta por Pagar).
3. doCredit: Identify credit terms. Use formats like: "Cash", "15 days", "30 days", "45 days", "60 days". 
   - If 'Plazo de crédito' is 0, use "Cash". 
   - If it is a number like '30', use "30 days".

Column "Código / Cód. CABYS" stacks SKU and CABYS. Separate them.

Return JSON:
{
    "header": {
        "doConsecutive": "string",
        "doDate": "YYYY-MM-DD",
        "doType": "FE, NC, TE, etc.",
        "doAccount": "CXC or CXP",
        "doCredit": "Cash, 30 days, etc.",
        "CurrencyID": "CRC or USD",
        "SubtotalAmount": 0.0,
        "TaxAmount": 0.0,
        "TotalAmount": 0.0,
        "issuer": {
            "cpName": "string or null",
            "cpTitle": "string or null",
            "cpIdentification": "string or null",
            "cpAddress": "string or null",
            "cpPhone": "string or null",
            "cpEmail": "string or null"
        },
        "receptor": {
            "cpName": "string or null",
            "cpTitle": "string or null",
            "cpIdentification": "string or null",
            "cpAddress": "string or null",
            "cpPhone": "string or null",
            "cpEmail": "string or null"
        }
    },
    "lines": [
        { 
            "sku_candidate": "string",
            "cabys_candidate": "string",
            "description": "string",
            "product_name": "string",
            "brand": "string",
            "model": "string",
            "quantity": 0.0, 
            "unit_price": 0.0,
            "discount_amount": 0.0,
            "subtotal_line": 0.0,
            "tax_amount": 0.0,
            "total_line": 0.0
        }
    ]
}"""

def extract_invoice_data(pdf_content_bytes):
    try:
        response = _client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_content_bytes, mime_type="application/pdf"),
                _PROMPT
            ],
            config=_json_config()
        )
        return _attach_usage(response, _parse_response_json(response))
    except Exception as e:
        logger.error(f"Error in extract_invoice_data: {str(e)}")
        return None

_COMPANY_PROMPT = """Extract the underlying company data from this document, specifically looking for the issuer or client details.
Focus on identifying the company that issued the invoice or the client it was billed to.

Return exactly one JSON object representing the most important company found in the document (usually the issuer).

Rules for names:
1. cpName: The official legal name (Razón Social). DO NOT include commercial names or names in parentheses here.
2. cpTitle: The commercial/trade name (Nombre de Fantasía). If the document has a name in parentheses or a distinct brand logo, put it here.

Return JSON format:
{
    "cpName": "string or null",
    "cpTitle": "string or null",
    "cpIdentification": "string or null",
    "cpAddress": "string or null",
    "cpEmail": "string or null",
    "cpPhone": "string or null"
}"""

def extract_company_data(pdf_content_bytes):
    try:
        response = _client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=pdf_content_bytes, mime_type="application/pdf"),
                _COMPANY_PROMPT
            ],
            config=_json_config()
        )
        return _attach_usage(response, _parse_response_json(response))
    except Exception as e:
        logger.error(f"Error in extract_company_data: {str(e)}")
        return None

_PRODUCT_URL_PROMPT = """You are a product catalog extraction assistant.
You will receive raw HTML from an e-commerce product page. Extract structured product data according to the following database hierarchy:

The input may include a "[STRUCTURED DATA JSON-LD]" block (schema.org data) and a "[PAGE TEXT]" block.
ALWAYS prefer the product's own description from the JSON-LD "description" field or the product detail
section of the page text.

1. Table 'bcItems' (PARENT): Generic master product.
   - itTitle: MUST be a generic, short name. NO brand, NO measures, NO capacity, NO specific material details. (e.g., 'Inodoro', 'Cemento', 'Adhesivo').
   - itDescription: A concise description of THIS specific product (its materials, use, features, specs),
     taken from the product's own description.
2. Table 'bcItemsLns' (CHILD): Specific variant/presentation.
   - itModel: MUST contain ALL identifying details of this specific variant (e.g., '2 Piezas 3.8 L Bone Olympus', 'Industrial Grade 50kg', 'Galón').

Rules:
1. itTitle (Generic Parent): Do not include specific technical specs here. Focus on the base entity name.
2. itModel (Specific Variant): This is where you put all the details like sizes, weights, colors, models, and specific versions.
3. itBrand: Identification of the manufacturer/brand.
4. itCategory/itSubcategory: Broad and specific classification. itCategory must be a short, generic category name (e.g. 'Herramientas', 'Pinturas', 'Construcción'), reusable across many products.
5. itSize: Physical dimensions/size as free text (e.g. '120x60 cm', '3/4 pulgada', '50 kg', '1.5 m'). Null if not stated.
6. itUnit: The unit of measure used to sell/count this product (e.g. 'Unidad', 'Galón', 'Saco', 'Caja', 'Metro', 'Kilogramo'). itUnitSymbol: its short symbol if known (e.g. 'Un', 'Gal', 'Saco', 'm', 'kg'). Default itUnit to 'Unidad' / itUnitSymbol 'Un' if the product is sold per piece and no other unit applies.
7. If a field is not found, use null.
8. Return plain text only (no HTML tags).
9. itDescription MUST describe the product itself. NEVER use the store's generic marketing/branding
   text, slogans, site-wide meta descriptions, menus/categories, breadcrumbs, or shipping/return/payment
   policies. Reject any text that promotes the store rather than the product (e.g. phrases like
   'E-commerce ferretería ... compra en línea', 'contamos con materiales para ...', store names).
   If no genuine product-specific description exists, set itDescription to null.

Return JSON:
{
    "itTitle": "string",
    "itDescription": "string",
    "itBrand": "string",
    "itCategory": "string",
    "itSubcategory": "string",
    "itModel": "string",
    "itSize": "string",
    "itUnit": "string",
    "itUnitSymbol": "string",
    "itObservations": "string"
}"""

def extract_product_from_html(html_text: str):
    try:
        relevant = extract_relevant_content(html_text)
        # Fallback al HTML crudo recortado si la limpieza no produjo nada
        trimmed_html = relevant or html_text[:50000]
        response = _client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[trimmed_html, _PRODUCT_URL_PROMPT],
            config=_json_config()
        )
        return _attach_usage(response, _parse_response_json(response))
    except Exception as e:
        logger.error(f"Error in extract_product_from_html: {str(e)}")
        return None

_PRODUCT_BARCODE_PROMPT = """You are a product catalog extraction assistant for a Costa Rican hardware/retail inventory system.
You are given a product BARCODE (EAN/UPC/GTIN). Use Google Search to identify the EXACT product this barcode belongs to, then extract structured product data according to the following database hierarchy.

Search strategy:
- Search the barcode number directly, and also combined with words like "producto", "ficha tecnica", "EAN", "UPC".
- Prefer official manufacturer pages, retailer product pages, and barcode databases.
- Cross-check that the brand/product is consistent across at least two sources before trusting it.

Database hierarchy:
1. Table 'bcItems' (PARENT): Generic master product.
   - itTitle: MUST be a generic, short name. NO brand, NO measures, NO capacity. (e.g., 'Inodoro', 'Cemento', 'Taladro').
   - itDescription: A concise description of THIS specific product (its materials, use, features, specs).
2. Table 'bcItemsLns' (CHILD): Specific variant/presentation.
   - itModel: MUST contain ALL identifying details of this specific variant (e.g., '2 Piezas 3.8 L Bone', 'Industrial 50kg', '20V 2Ah').

Rules:
1. itTitle (Generic Parent): base entity name only, no specs.
2. itModel (Specific Variant): all the details (sizes, weights, colors, models, versions).
3. itBrand: manufacturer/brand.
4. itCategory/itSubcategory: broad and specific classification. itCategory must be a short, generic category name (e.g. 'Herramientas', 'Pinturas'), reusable across many products.
5. itSize: physical dimensions/size as free text (e.g. '120x60 cm', '3/4 pulgada', '50 kg'). Null if not stated.
6. itUnit: unit of measure used to sell/count this product (e.g. 'Unidad', 'Galón', 'Saco', 'Caja'). itUnitSymbol: its short symbol (e.g. 'Un', 'Gal', 'Saco'). Default to 'Unidad'/'Un' if sold per piece.
7. image_url: a direct https URL to a product image if found, else null.
8. itWebsite: the URL of the best source page used, else null.
9. Return plain text only (no HTML tags).
10. itDescription MUST describe the product itself, never the store's generic marketing/slogans/policies. If no genuine product description exists, set it to null.
11. CRITICAL: If you are NOT confident the barcode matches a real, specific product, set itTitle to null and all fields to null. NEVER invent or guess a product. Returning nulls is better than returning wrong data.

Return ONLY a JSON object (no markdown fences, no extra text):
{
    "itTitle": "string or null",
    "itDescription": "string or null",
    "itBrand": "string or null",
    "itCategory": "string or null",
    "itSubcategory": "string or null",
    "itModel": "string or null",
    "itSize": "string or null",
    "itUnit": "string or null",
    "itUnitSymbol": "string or null",
    "itObservations": "string or null",
    "image_url": "string or null",
    "itWebsite": "string or null"
}"""


def _parse_json_object(raw: str):
    """Extrae el primer objeto JSON de la respuesta del modelo.

    Con la herramienta google_search NO se puede forzar response_mime_type=application/json,
    así que el texto puede traer fences ```json o explicaciones alrededor del JSON.
    """
    if not raw:
        return None
    cleaned = raw.replace('```json', '').replace('```', '').strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


_UNIT_INFER_PROMPT = """You classify the UNIT OF MEASURE used to SELL/COUNT a Costa Rican hardware/retail product, given its name and variant text.

Return the SELLING unit, not a physical dimension. Most discrete products are sold per piece -> use 'Unidad' / 'Un'.
Use a more specific unit ONLY when the product is clearly sold that way:
- Liquids / paint: 'Galón'/'Gal', 'Litro'/'L', 'Cubeta'/'Cub'
- Bagged / bulk: 'Saco'/'Saco', 'Bolsa'/'Bolsa', 'Kilogramo'/'kg'
- Packaged sets: 'Caja'/'Cj'
- Sold by length: 'Metro'/'m', 'Metro Cúbico'/'m3'
- Sheets / boards: 'Lámina'/'Lám', 'Hoja'/'Hoja'
- Rolls: 'Rollo'/'Rollo'
A measure like '3.05 m' or '1.82 X 2.44 m' inside the variant text is usually a DIMENSION, not the selling unit;
default to 'Unidad' unless the product is genuinely sold by that measure (e.g. cable/rope by the meter).
When in doubt, return 'Unidad'/'Un'.

Return ONLY a JSON object (no markdown fences):
{"itUnit": "string", "itUnitSymbol": "string"}"""

def infer_unit_from_text(title: str, model: str):
    """Infiere la unidad de venta (itUnit/itUnitSymbol) a partir del nombre y la variante
    que ya están en la base de datos, sin re-scrapear. Devuelve None si no hay texto."""
    text_in = f"NAME: {title or ''}\nVARIANT: {model or ''}".strip()
    if not (title or model):
        return None
    try:
        response = _client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[text_in, _UNIT_INFER_PROMPT],
            config=_json_config()
        )
        return _parse_response_json(response)
    except Exception as e:
        logger.error(f"Error in infer_unit_from_text: {str(e)}")
        return None

_SIZE_INFER_PROMPT = """You extract the PHYSICAL DIMENSIONS / SIZE of a Costa Rican hardware/retail product
from its name and variant text, and return it as free text (the value for the 'Dimensión' field).

Extract ONLY measurable size data: thickness, length, width, height, diameter, gauge, volume, weight, area.
Valid examples: '2.5 mm 1.82 X 2.44 m', '35 mm', '1/2" A 3/8"', '1 X 1" x 3.05 m Calibre #25', '3.8 L', '50 kg'.

Rules:
1. Extract ONLY genuine physical measurements. DO NOT include brand, color, material, model name, or product type
   (e.g. from '35 mm Caoba' return '35 mm'; from '3.8 L Bone Olympus' return '3.8 L').
2. Keep the original units/notation as written (mm, cm, m, ", L, kg, Calibre, etc.).
3. If the text has NO physical dimension/size, return null. NEVER invent or guess measurements.

Return ONLY a JSON object (no markdown fences): {"itSize": "string or null"}"""

def infer_size_from_text(title: str, model: str):
    """Extrae la dimensión física (itSize) a partir del nombre y la variante que ya están en la BD,
    sin re-scrapear. Devuelve {'itSize': None} cuando el producto no tiene medidas (se debe saltar)."""
    text_in = f"NAME: {title or ''}\nVARIANT: {model or ''}".strip()
    if not (title or model):
        return None
    try:
        response = _client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[text_in, _SIZE_INFER_PROMPT],
            config=_json_config()
        )
        return _parse_response_json(response)
    except Exception as e:
        logger.error(f"Error in infer_size_from_text: {str(e)}")
        return None

_CLEAN_TITLE_PROMPT = """You clean the PARENT product name (itTitle) of a Costa Rican hardware/retail catalog item.
The parent name must be the GENERIC, short product name. The specific details already live in other fields
(brand, dimension/size, variant/model), so they must be REMOVED from the name to avoid duplication.

REMOVE from the name:
- Physical measurements / dimensions: '3 mm', '1.22 x 10 m', '60 X 45 cm', '5 L', '50 kg', '1/2"', 'Calibre #25', etc.
- The brand name.
- Quantities / presentation: '2 Piezas', '10 Piezas', '50 Unidades', 'Galon', 'Litro', etc.
- Color / finish / material that only distinguishes a variant: 'Gris', 'Caoba', 'Cedro Natural', 'Blanco',
  'Negro', 'Hierro Negro', 'Satin', 'Miel', 'Transparente', 'Bone', etc.

KEEP:
- The base product name and core product-line / type codes that identify the product family (e.g. 'A-3', 'AP10').

Example: 'Aislante Termico A-3 3 mm 1.22 x 10 m Prodex' -> 'Aislante Termico A-3'

You are given the current name plus the values stored in the other fields (use them as hints of what to strip).
Return the cleaned generic name. If it is already clean, return it unchanged. Never return an empty string.

Return ONLY a JSON object (no markdown fences): {"itTitle": "string"}"""

def clean_parent_title(title: str, brand: str = "", size: str = "", model: str = ""):
    """Limpia el nombre del padre (itTitle) dejando el nombre genérico, quitando dimensión, marca,
    presentación y color/acabado de variante. Devuelve None ante error o título vacío."""
    title = str(title or "").strip()
    if not title:
        return None
    context = (
        f"CURRENT NAME: {title}\n"
        f"BRAND (remove if present): {brand or '-'}\n"
        f"DIMENSION (remove if present): {size or '-'}\n"
        f"VARIANT/MODEL (remove if present): {model or '-'}"
    )
    try:
        response = _client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[context, _CLEAN_TITLE_PROMPT],
            config=_json_config()
        )
        return _parse_response_json(response)
    except Exception as e:
        logger.error(f"Error in clean_parent_title: {str(e)}")
        return None

def extract_product_from_barcode(barcode: str):
    """Resuelve un código de barras (EAN/UPC/GTIN) a datos de producto usando Gemini + Google Search.

    Devuelve el mismo esquema JSON que extract_product_from_html (más image_url e itWebsite),
    de modo que create_item_from_url_logic puede consumirlo sin cambios. Retorna None si Gemini
    no encuentra el producto o no está seguro (no inventa datos).
    """
    barcode = (barcode or "").strip()
    if not barcode:
        return None
    try:
        # Grounding con búsqueda web; incompatible con response_mime_type=application/json.
        search_tool = types.Tool(google_search=types.GoogleSearch())
        prompt = f"{_PRODUCT_BARCODE_PROMPT}\n\nBARCODE: {barcode}"
        response = _client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[prompt],
            config=types.GenerateContentConfig(
                tools=[search_tool],
                temperature=0.1,
            )
        )
        data = _parse_json_object(response.text)
        if not data:
            return None
        return _attach_usage(response, data)
    except Exception as e:
        logger.error(f"Error in extract_product_from_barcode: {str(e)}")
        return None

from google import genai
from google.genai import types
import os
import json
from dotenv import load_dotenv

load_dotenv()

_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

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
            "cpName": "string or null - Official Legal Name (Razón Social)",
            "cpTitle": "string or null - Trade / Fantasy Name (Nombre Comercial/Fantasía)",
            "cpIdentification": "string or null",
            "cpAddress": "string or null",
            "cpPhone": "string or null",
            "cpEmail": "string or null"
        },
        "receptor": {
            "cpName": "string or null - Official Legal Name (Razón Social)",
            "cpTitle": "string or null - Trade / Fantasy Name (Nombre Comercial/Fantasía)",
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
            "product_name": "string (Generic)",
            "brand": "string (Extract brand name if present)",
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
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=pdf_content_bytes, mime_type="application/pdf"),
                _PROMPT
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=0 
                ),
            )
        )
        
        text = response.text.replace('```json', '').replace('```', '')
        data = json.loads(text)
        
        usage = response.usage_metadata
        print(f"DEBUG: Usage Metadata from Gemini: {usage}")
        
        data['usage'] = {
            'prompt_tokens': usage.prompt_token_count,
            'candidates_tokens': usage.candidates_token_count,
            'total_tokens': usage.total_token_count
        }
        print(f"DEBUG: Data with usage: {data['usage']}")
        
        return data
    except Exception as e:
        print(f"Error parsing Gemini JSON: {e}")
        return None

_COMPANY_PROMPT = """Extract the underlying company data from this document, specifically looking for the issuer or client details.
Focus on identifying the company that issued the invoice or the client it was billed to.

Return exactly one JSON object representing the most important company found in the document (usually the issuer).

Rules for names:
1. cpName: The official legal name (Razón Social). DO NOT include commercial names or names in parentheses here.
2. cpTitle: The commercial/trade name (Nombre de Fantasía). If the document has a name in parentheses or a distinct brand logo, put it here.

Return JSON format:
{
    "cpName": "string or null - Official legal name (e.g. 'Grupo Istmo de Papagayo S.R.L.')",
    "cpTitle": "string or null - Commercial name (e.g. 'Hotel Four Seasons')",
    "cpIdentification": "string or null - Tax ID / VAT number",
    "cpAddress": "string or null - Full address",
    "cpEmail": "string or null - Primary contact email",
    "cpPhone": "string or null - Primary contact phone number"
}"""

def extract_company_data(pdf_content_bytes):
    try:
        response = _client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=pdf_content_bytes, mime_type="application/pdf"),
                _COMPANY_PROMPT
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=0 
                ),
            )
        )
        
        text = response.text.replace('```json', '').replace('```', '')
        data = json.loads(text)
        
        usage = response.usage_metadata
        data['usage'] = {
            'prompt_tokens': usage.prompt_token_count,
            'candidates_tokens': usage.candidates_token_count,
            'total_tokens': usage.total_token_count
        }
        
        return data
    except Exception as e:
        print(f"Error parsing Gemini JSON for company data: {e}")
        return None

_PRODUCT_URL_PROMPT = """You are a product catalog extraction assistant.
You will receive raw HTML from an e-commerce product page. Extract all available structured product data.

Rules:
1. If a field is not found, use null.
2. For the price, extract only the numeric value (no currency symbols).
3. Brand should be the manufacturer/brand of the product, not the store.
4. Category should be a broad category (e.g., Spirits, Wine, Beer, Electronics, etc.).
5. Subcategory should be a more specific type (e.g., Gin, Bourbon, IPA, etc.).
6. model should be the specific variant or presentation (e.g., '700ml', 'Wild Berry').
7. Extract all tags/labels if available.

Return JSON format:
{
    "itTitle": "Generic product name (without brand or size)",
    "itDescription": "Full product description",
    "itBrand": "Brand name",
    "itCategory": "Broad category",
    "itSubcategory": "Specific subcategory",
    "itModel": "Variant or presentation (e.g. 700ml, Wild Berry)",
    "itObservations": "Additional notes, tasting notes, aromas, etc."
}"""

def extract_product_from_html(html_text: str):
    """Uses Gemini to extract structured product data from scraped HTML."""
    try:
        # Trim HTML to avoid exceeding token limits
        # We keep first 50k chars which usually covers the product detail section
        trimmed_html = html_text[:50000]
        
        response = _client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[trimmed_html, _PRODUCT_URL_PROMPT],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=0
                ),
            )
        )
        
        text = response.text.replace('```json', '').replace('```', '')
        data = json.loads(text)
        
        usage = response.usage_metadata
        data['usage'] = {
            'prompt_tokens': usage.prompt_token_count,
            'candidates_tokens': usage.candidates_token_count,
            'total_tokens': usage.total_token_count
        }
        return data
    except Exception as e:
        print(f"Error parsing Gemini JSON for product URL data: {e}")
        return None

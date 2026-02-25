from google import genai
from google.genai import types
import os
import json
from dotenv import load_dotenv

load_dotenv()

_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

_PROMPT = """Extract data from this Costa Rican invoice PDF. Be extremely precise with financial totals and taxes.

Column "Código / Cód. CABYS" stacks SKU (e.g. 'GCP') and CABYS ('2413...'). Separate them.

Return JSON:
{
    "header": {
        "doConsecutive": "string",
        "doDate": "YYYY-MM-DD",
        "doType": "FE or NC",
        "CurrencyID": "CRC or USD",
        "SubtotalAmount": 0.0,
        "TaxAmount": 0.0,
        "TotalAmount": 0.0,
        "issuer": {
            "cpName": "string or null - Official legal name",
            "cpTitle": "string or null - Commercial name",
            "cpIdentification": "string or null - Tax ID / Corporate ID",
            "cpAddress": "string or null - Full address",
            "cpPhone": "string or null",
            "cpEmail": "string or null"
        },
        "receptor": {
            "cpName": "string or null - Official legal name",
            "cpTitle": "string or null - Commercial name",
            "cpIdentification": "string or null - Tax ID / Corporate ID",
            "cpAddress": "string or null - Full address",
            "cpPhone": "string or null",
            "cpEmail": "string or null"
        }
    },
    "lines": [
        { 
            "sku_candidate": "string",
            "cabys_candidate": "string",
            "description": "string - Full original description",
            "product_name": "string - Generic product name (e.g. 'Coca Cola Zero', 'Giffard Pineapple')",
            "variant_name": "string - Specific variant name (e.g. 'Coca Cola Zero')",
            "size": "string or null - Size or weight (e.g. '2L', '750 ml')",
            "quantity": 0.0, 
            "unit_price": 0.0,
            "subtotal_line": 0.0,
            "discount_amount": 0.0,
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

Return JSON format:
{
    "cpName": "string or null - The official legal name of the company",
    "cpTitle": "string or null - The commercial/trade name if different from legal name",
    "cpIdentification": "string or null - The tax ID, VAT number, or corporate identification number",
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

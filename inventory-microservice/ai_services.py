from google import genai
from google.genai import types
import os
import json
from dotenv import load_dotenv

load_dotenv()

# Nuevo SDK: google-genai (reemplaza google.generativeai deprecado)
_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

_PROMPT = """Extract data from this Costa Rican invoice PDF.

Column "Código / Cód. CABYS" stacks SKU (e.g. 'GCP') and CABYS ('2413...'). Separate them.

Return JSON:
{
    "header": {
        "doConsecutive": "string",
        "doDate": "YYYY-MM-DD",
        "doIssuerID": "string", 
        "doIssuerName": "string",
        "doType": "FE or NC"
    },
    "lines": [
        { 
            "sku_candidate": "string",
            "cabys_candidate": "string",
            "description": "string", 
            "quantity": 0.0, 
            "unit_price": 0.0,
            "discount_amount": 0.0,
            "tax_amount": 0.0
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
                    thinking_budget=0  # Sin razonamiento = respuesta directa
                ),
            )
        )
        
        text = response.text.replace('```json', '').replace('```', '')
        data = json.loads(text)
        
        # Capturar uso de tokens
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

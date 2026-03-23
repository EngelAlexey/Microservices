import logging
import os
import sys

# Add current dir to path
sys.path.append(os.path.abspath('.'))

from drive_services import get_drive_service, download_with_validation
from ai_services import extract_invoice_data

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s',
    stream=sys.stdout
)

def run_tests():
    print("--- TESTING SPECIFIC DRIVE ID ---")
    drive_id = "1ouuXVogN-X6qCZXFljZ7Hk2v5LEnh16G"
    print(f"Target ID: {drive_id}")
    
    print("\n1. Attempting Download...")
    content, meta = download_with_validation(drive_id)
    
    if content:
        print(f"SUCCESS: Downloaded file '{meta.get('name')}' ({len(content)} bytes)")
        
        print("\n2. Attempting AI Extraction...")
        data = extract_invoice_data(content)
        if data:
            print("SUCCESS: AI extracted data successfully.")
            import json
            print(json.dumps(data.get('header', {}), indent=2))
        else:
            print("FAILURE: AI extraction failed (Check GEMINI_API_KEY or model name).")
    else:
        print("FAILURE: Could not download file. Check service account permissions or if file is truly public.")

if __name__ == "__main__":
    run_tests()

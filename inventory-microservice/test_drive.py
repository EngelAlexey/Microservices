import logging
import os
import sys

# Add current dir to path
sys.path.append(os.path.abspath('.'))

from drive_services import get_drive_service, resolve_file_id

# Configure logging to be very explicit
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s',
    stream=sys.stdout
)

def run_tests():
    print("--- STARTING TESTS ---")
    
    print("\n1. Checking Credentials...")
    service = get_drive_service()
    if service:
        print("RESULT: Service account loaded and service built.")
    else:
        print("RESULT: ERROR - Service account failed to load.")

    print("\n2. Testing Path Resolution...")
    path = "B01-Bodegas Benjamín/Documents/70AD993F64.doFile.170533.pdf"
    print(f"INPUT PATH: {path}")
    result = resolve_file_id(path)
    print(f"OUTPUT: {result}")
    
    if result == path:
        print("RESULT: Path could NOT be resolved to an ID.")
    else:
        print(f"RESULT: Successfully resolved to ID: {result}")

if __name__ == "__main__":
    run_tests()

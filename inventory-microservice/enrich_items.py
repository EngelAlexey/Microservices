import sys
import os
import time
import requests
import asyncio
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from database import SessionLocal
from models import BcItem
from logic import create_item_from_url_logic
from scrape_services import scrape_product_page
from ai_services import extract_product_from_html

load_dotenv()

SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
DEFAULT_IMAGE_FOLDER_ID = os.environ.get("DEFAULT_IMAGE_FOLDER_ID")
DATABASE_ID = "KZN"

def find_specific_url(query: str):
    """Busa la URL específica del producto en ellagar.com usando Serper."""
    if not SERPER_API_KEY:
        print("Missing SERPER_API_KEY!")
        return None
        
    search_url = "https://google.serper.dev/search"
    headers = {
        'X-API-KEY': SERPER_API_KEY,
        'Content-Type': 'application/json'
    }
    # Forzamos la búsqueda solo en la página de Ellagar
    payload = {
        "q": f"site:ellagar.com {query}",
        "num": 3
    }
    
    try:
        response = requests.post(search_url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            data = response.json()
            results = data.get("organic", [])
            for res in results:
                link = res.get("link", "")
                # Asegurarnos de que no sea una categoría general, sino un artículo específico
                # Si el link contiene algo como "/CategoriaArticulo/", podemos omitirlo si queremos ser estrictos
                # Pero en general el primer resultado orgánico a un producto suele ser el más acertado.
                if link.startswith("https://www.ellagar.com/"):
                    return link
    except Exception as e:
        print(f"Error calling Serper for {query}: {e}")
        
    return None

async def process_item(item: BcItem):
    """Enriquece un solo ítem llamando a la lógica de URL de main.py"""
    query = f"{item.itTitle} {item.itBrand or ''}".strip()
    print(f"\n--- Procesando: {query} (ID: {item.ItemID}) ---")
    
    specific_url = find_specific_url(query)
    if not specific_url:
        print(f"  -> No se encontró URL específica para '{query}'")
        return False
        
    print(f"  -> URL Encontrada: {specific_url}")
    
    # Scrape HTML and Image
    html, image_url = scrape_product_page(specific_url)
    if not html:
        print(f"  -> Fallo raspando {specific_url}")
        return False
        
    print(f"  -> HTML scrapeado ({len(html)} bytes). Imagen encontrada: {image_url}")
    
    # Extraer Datos con IA
    print("  -> Extrayendo datos con Gemini...")
    data = extract_product_from_html(html)
    if not data:
        print("  -> Fallo extracción IA")
        return False
        
    # Re-inyectamos el itWebsite real
    data['itWebsite'] = specific_url
    
    # Actualizar base de datos
    db = SessionLocal()
    try:
        # Se reutiliza la lógica original que además sube a Drive en background!
        print("  -> Ejecutando create_item_from_url_logic...")
        result = create_item_from_url_logic(
            db, data, 
            image_url=image_url, 
            database_id=DATABASE_ID, 
            image_folder_id=DEFAULT_IMAGE_FOLDER_ID,
            item_id=item.ItemID
        )
        print(f"  -> Item actualizado con éxito: {result.get('action')}")
        return True
    except Exception as e:
        print(f"  -> Error guardando en DB: {e}")
        return False
    finally:
        db.close()

async def main():
    import json
    seed_path = os.path.join(os.path.dirname(__file__), "test", "seed", "seed_ellagar_50.json")
    with open(seed_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    # Construir un objeto falso tipo BcItem para reutilizar process_item
    # o mejor modificar un poco process_item
    # Simplifiquemos pasando un dict a process_item_dict
    items_data = data.get("bcItems", [])
    
    print(f"Se encontraron {len(items_data)} items en el archivo seed para enriquecer e insertar.")
    
    success_count = 0
    for i_data in items_data:
        # Simulamos un objeto para process_item
        class FakeItem:
            def __init__(self, d):
                self.ItemID = d.get("ItemID")
                self.itTitle = d.get("itTitle")
                self.itBrand = d.get("itBrand", "")
        
        simulated_item = FakeItem(i_data)
        try:
            success = await process_item(simulated_item)
            if success:
                success_count += 1
        except Exception as err:
            import traceback
            print(f"ERROR GORDO en {simulated_item.itTitle}: {err}")
            traceback.print_exc()
        # Pausa de 2 segundos para rate limiting
        time.sleep(2)
        
    print(f"\nProcesamiento terminado. Éxitos: {success_count}/{len(items_data)}")

if __name__ == "__main__":
    asyncio.run(main())

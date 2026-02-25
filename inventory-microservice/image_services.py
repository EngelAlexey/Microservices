import requests
import logging
import os

logger = logging.getLogger(__name__)

def search_product_image(query: str) -> tuple[bytes, str, str]:
    """Busca una imagen usando Serper.dev (Google Search API) y retorna los bytes."""
    try:
        api_key = os.environ.get("SERPER_API_KEY")
        
        if not api_key:
            logger.error("Falta SERPER_API_KEY en las variables de entorno.")
            return None, None, None

        logger.info(f"Buscando imagen en Serper.dev para: {query}")
        
        search_url = "https://google.serper.dev/images"
        headers = {
            'X-API-KEY': api_key,
            'Content-Type': 'application/json'
        }
        payload = {
            "q": query,
            "num": 5
        }
        
        response = requests.post(search_url, headers=headers, json=payload, timeout=10)
        
        if response.status_code != 200:
            logger.error(f"Error de Serper API: {response.status_code}")
            return None, None, None
            
        data = response.json()
        items = data.get("images", [])
        
        if not items:
            logger.warning(f"No se encontraron imágenes para: {query}")
            return None, None, None
            
        for item in items:
            img_url = item.get("imageUrl")
            img_title = item.get("title", "").lower()
            
            # Verificación 'Infalible': El título del resultado debe tener al menos una palabra clave
            query_keywords = [w for w in query.lower().split() if len(w) > 3]
            if query_keywords and not any(kw in img_title for kw in query_keywords):
                logger.debug(f"Saltando imagen (título no coincide): {img_title}")
                continue

            try:
                # Descargamos simulando un navegador simple
                download_headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
                img_response = requests.get(img_url, headers=download_headers, timeout=10)
                
                if img_response.status_code == 200:
                    content_type = img_response.headers.get('Content-Type', '')
                    ext = "png"
                    if "jpeg" in content_type or "jpg" in content_type:
                        ext = "jpg"
                    
                    logger.info(f"Imagen descargada con éxito: {img_url}")
                    return img_response.content, content_type, ext
            except Exception as e:
                logger.error(f"Error descargando {img_url}: {e}")
                continue
                
        return None, None, None
        
    except Exception as e:
        logger.error(f"Excepción en búsqueda de imagen: {e}")
        return None, None, None

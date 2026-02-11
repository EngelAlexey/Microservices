from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
import os
import functools

# SCOPE DE SOLO LECTURA (Seguridad Máxima)
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
SERVICE_ACCOUNT_FILE = 'service_account.json'

@functools.lru_cache(maxsize=1)
def get_drive_service():
    """Singleton: El servicio de Drive se crea UNA sola vez y se reutiliza."""
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print("ADVERTENCIA: No se encontró service_account.json")
        return None
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds)

def download_with_validation(file_id):
    """Descarga el archivo y obtiene metadata en una sola operación.
    
    Returns:
        tuple: (file_bytes, metadata_dict) o (None, None) si falla.
    """
    try:
        service = get_drive_service()
        if not service:
            return None, None
        
        # Obtener metadata (validación)
        meta = service.files().get(fileId=file_id, fields="name, mimeType").execute()
        
        # Descargar contenido (mismo servicio, sin reconstruir)
        request = service.files().get_media(fileId=file_id)
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, request)
        
        done = False
        while done is False:
            status, done = downloader.next_chunk()
            
        file_stream.seek(0)
        return file_stream.read(), meta
    except Exception as e:
        print(f"Error en Drive: {e}")
        return None, None

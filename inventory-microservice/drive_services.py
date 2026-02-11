from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
import os
import functools

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
SERVICE_ACCOUNT_FILE = 'service_account.json'

@functools.lru_cache(maxsize=1)
def get_drive_service():
    # Check Render secrets first
    possible_paths = [
        "/etc/secrets/service_account",       # Render secret filename (often without extension)
        "/etc/secrets/service_account.json",  # Render secret filename (if user added extension)
        'service_account.json'                # Local development
    ]
    
    creds_file = None
    for path in possible_paths:
        if os.path.exists(path):
            creds_file = path
            break
            
    if not creds_file:
        print(f"ADVERTENCIA: No se encontró service_account.json en ninguna de las rutas: {possible_paths}")
        return None
        
    try:
        creds = service_account.Credentials.from_service_account_file(
            creds_file, scopes=SCOPES)
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"Error cargando credenciales desde {creds_file}: {e}")
        return None

def download_with_validation(file_id):
    
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

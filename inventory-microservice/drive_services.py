import io
import os
import logging
import functools
import time
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_FILE = 'service_account.json'

def get_drive_service(impersonate_user: str = None):
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
        logger.warning(f"No se encontró service_account.json en ninguna de las rutas: {possible_paths}")
        return None
        
    try:
        creds = service_account.Credentials.from_service_account_file(
            creds_file, scopes=SCOPES)
            
        # Si se solicita impersonación (Domain-Wide Delegation)
        if impersonate_user:
            try:
                creds = creds.with_subject(impersonate_user)
            except Exception as dwd_err:
                logger.error(f"Falló la configuración de delegación para {impersonate_user}: {dwd_err}")
                
        return build('drive', 'v3', credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error(f"Error cargando credenciales desde {creds_file}: {e}")
        return None

def resolve_file_id(file_id_or_path: str) -> str:
    """
    Si file_id_or_path parece una ruta de AppSheet (contiene '/'),
    busca el archivo en Drive por nombre y retorna su ID real.
    De lo contrario retorna el file_id tal cual.
    """
    if not file_id_or_path or '/' not in file_id_or_path:
        return file_id_or_path  # Ya es un Drive ID real
    
    # Es una ruta de AppSheet estilo /Documents/filename.pdf
    filename = file_id_or_path.strip('/').split('/')[-1]
    logger.info(f"Resolviendo ruta AppSheet '{file_id_or_path}' -> buscando '{filename}' en Drive...")
    
    try:
        service = get_drive_service()
        if not service:
            return file_id_or_path
        
        # Buscar por nombre exacto en todos los drives
        query = f"name = '{filename}' and trashed = false"
        results = service.files().list(
            q=query,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=5
        ).execute()
        
        files = results.get('files', [])
        if files:
            real_id = files[0]['id']
            logger.info(f"Archivo encontrado en Drive: {real_id} ({filename})")
            return real_id
        else:
            logger.error(f"No se encontró '{filename}' en Drive.")
            return file_id_or_path  # Retornar original para que falle con error claro
    except Exception as e:
        logger.error(f"Error resolviendo ruta de Drive: {e}")
        return file_id_or_path

def download_with_validation(file_id):
    """
    Returns:
        tuple: (file_bytes, metadata_dict) o (None, None) si falla.
    """
    try:
        service = get_drive_service()
        if not service:
            return None, None
        
        # Obtener metadata (validación)
        meta = service.files().get(fileId=file_id, fields="name, mimeType").execute()
        
        # Descargar contenido
        request = service.files().get_media(fileId=file_id)
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, request)
        
        done = False
        while done is False:
            _, done = downloader.next_chunk()
            
        file_stream.seek(0)
        return file_stream.read(), meta
    except Exception as e:
        logger.error(f"Error en descarga de Drive: {e}")
        return None, None

@functools.lru_cache(maxsize=128)
def get_folder_path_from_drive(folder_id: str) -> str:
    """
    Recupera la ruta completa recursiva dado el ID de Drive.
    """
    try:
        service = get_drive_service()
        if not service:
            return ""
            
        path_parts = []
        current_id = folder_id

        while current_id:
            # Obtener metadata del elemento actual
            item = service.files().get(
                fileId=current_id, 
                fields="id, name, parents, driveId", 
                supportsAllDrives=True
            ).execute()
            
            name = item.get('name')
            parents = item.get('parents', [])
            drive_id = item.get('driveId')

            path_parts.insert(0, name)
            
            if drive_id and (not parents or parents[0] == drive_id):
                try:
                    drive_info = service.drives().get(driveId=drive_id).execute()
                    drive_name = drive_info.get('name')
                    if drive_name and drive_name != name:
                        path_parts.insert(0, drive_name)
                except:
                    pass
                break
            
            if not parents:
                break
            current_id = parents[0]

        if path_parts:
            return "/".join(path_parts) + "/"
        return ""
    except Exception as e:
        logger.error(f"Error obteniendo path recursivo para {folder_id}: {e}")
        return ""

def upload_image_to_drive(image_bytes, filename, mime_type, folder_id):
    """Sube los bytes de una imagen a la carpeta de Drive especificada con reintentos."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            service = get_drive_service()
            if not service:
                return None
                
            file_metadata = {
                'name': filename,
                'parents': [folder_id]
            }
            
            media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype=mime_type, resumable=False)
            
            file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id',
                supportsAllDrives=True
            ).execute()
            
            file_id = file.get('id')
            
            # Dar permisos de lectura públicos (necesario para AppSheet)
            try:
                service.permissions().create(
                    fileId=file_id,
                    body={'type': 'anyone', 'role': 'reader'},
                    supportsAllDrives=True
                ).execute()
            except Exception as pe:
                logger.warning(f"No se pudieron otorgar permisos públicos a {file_id}: {pe}")

            return file_id
            
        except Exception as e:
            logger.error(f"Error en intento {attempt + 1} de subida a Drive: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                logger.error(f"Fallo crítico tras {max_retries} intentos de subida.")
                return None

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import io
import os
import functools

SCOPES = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_FILE = 'service_account.json'

@functools.lru_cache(maxsize=1)
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
        print(f"ADVERTENCIA: No se encontró service_account.json en ninguna de las rutas: {possible_paths}")
        return None
        
    try:
        creds = service_account.Credentials.from_service_account_file(
            creds_file, scopes=SCOPES)
            
        print(f"[{'DWD' if impersonate_user else 'NORMAL'}] Autenticando Drive...")
        # Si se solicita impersonación (Domain-Wide Delegation)
        if impersonate_user:
            try:
                # Esto fallará si la cuenta de servicio no tiene Domain-Wide Delegation
                # configurado en el panel de administrador de Google Workspace.
                creds = creds.with_subject(impersonate_user)
                print(f"Drive API usará la identidad y espacio de: {impersonate_user}")
            except Exception as dwd_err:
                print(f"ADVERTENCIA: Falló la configuración de delegación para {impersonate_user}: {dwd_err}")
                
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"Error cargando credenciales desde {creds_file}: {e}")
        return None

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

@functools.lru_cache(maxsize=128)
def get_folder_path_from_drive(folder_id: str) -> str:
    """
    Recupera el nombre de la carpeta, de su carpeta padre y de su abuelo dado el ID de Drive.
    Retorna el string ensamblado ej: 'Kaizen/A14-Bodegas Benjamin/Items/'
    """
    try:
        service = get_drive_service()
        if not service:
            return ""
            
        # 1. Obtener carpeta actual (Child)
        folder = service.files().get(fileId=folder_id, fields="name, parents", supportsAllDrives=True).execute()
        folder_name = folder.get('name')
        parents = folder.get('parents', [])
        
        parent_name = ""
        grandparent_name = ""
        
        if parents:
            # 2. Obtener carpeta padre (Parent)
            parent_id = parents[0]
            parent_folder = service.files().get(fileId=parent_id, fields="name, parents", supportsAllDrives=True).execute()
            parent_name = parent_folder.get('name', '')
            grandparents = parent_folder.get('parents', [])
            
            if grandparents:
                # 3. Obtener carpeta abuelo (Grandparent)
                grandparent_id = grandparents[0]
                grandparent_folder = service.files().get(fileId=grandparent_id, fields="name", supportsAllDrives=True).execute()
                grandparent_name = grandparent_folder.get('name', '')
            
        # Ensamblar path
        parts = [p for p in [grandparent_name, parent_name, folder_name] if p]
        if parts:
            return "/".join(parts) + "/"
        return ""
    except Exception as e:
        print(f"Error obteniendo path para {folder_id}: {e}")
        return ""


# Eliminado search_product_image redundante de aquí


def upload_image_to_drive(image_bytes, filename, mime_type, folder_id):
    """Sube los bytes de una imagen a la carpeta de Drive especificada."""
    try:
        print(f"    [DRIVE] Iniciando subida de {filename} a {folder_id}...")
        service = get_drive_service()
        if not service:
            print("    [DRIVE] ERROR: No se pudo obtener el servicio de Drive.")
            return None
            
        file_metadata = {
            'name': filename,
            'parents': [folder_id]
        }
        
        # Crear stream de bytes
        from io import BytesIO
        media = MediaIoBaseUpload(BytesIO(image_bytes), mimetype=mime_type, resumable=False)
        
        # supportsAllDrives is required for Team Drives / Shared Drives
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webContentLink',
            supportsAllDrives=True
        ).execute()
        
        file_id = file.get('id')
        print(f"    [DRIVE] ¡Subida exitosa! ID: {file_id}")
        
        # Dar permisos de lectura a cualquiera con el link (necesario para AppSheet)
        try:
            service.permissions().create(
                fileId=file_id,
                body={'type': 'anyone', 'role': 'reader'},
                supportsAllDrives=True
            ).execute()
            print(f"    [DRIVE] Permisos públicos otorgados a {file_id}")
        except Exception as pe:
            print(f"    [DRIVE] ADVERTENCIA Permisos: {pe}")

        return file.get('webContentLink')
        
    except Exception as e:
        print(f"    [DRIVE] ERROR CRITICO: {e}")
        return None

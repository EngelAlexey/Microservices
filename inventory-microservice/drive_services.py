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
    Recupera la ruta completa recursiva dado el ID de Drive.
    Retorna el string ensamblado ej: '03-Aplicaciones/11- Automatizaciones/Imagenes-AI/'
    """
    try:
        service = get_drive_service()
        if not service:
            return ""
            
        path_parts = []
        current_id = folder_id
        drive_name = None

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
            
            # Si tiene un driveId y no hay padres, o el padre es el mismo drive
            # intentamos obtener el nombre de la Unidad Compartida (Root)
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
            # Aseguramos que el path termine en /
            return "/".join(path_parts) + "/"
        return ""
    except Exception as e:
        print(f"Error obteniendo path recursivo para {folder_id}: {e}")
        return ""


# Eliminado search_product_image redundante de aquí


def upload_image_to_drive(image_bytes, filename, mime_type, folder_id):
    """Sube los bytes de una imagen a la carpeta de Drive especificada con reintentos."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"    [DRIVE] Intento {attempt + 1}/{max_retries}: Subiendo {filename} a {folder_id}...")
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
                # Si falla aquí, probablemente es por restricciones de la unidad compartida (canShare=False)
                # No reintentamos todo el upload por esto, solo avisamos.
                print(f"    [DRIVE] ADVERTENCIA Permisos: No se pudo hacer público ({pe}). Verifique configuración de la Unidad Compartida.")

            return file.get('webContentLink')
            
        except Exception as e:
            # Si el error es de permisos al crear el archivo, se capturará aquí
            print(f"    [DRIVE] ERROR en intento {attempt + 1}: {e}")
            if "insufficientFilePermissions" in str(e) or "sharing" in str(e).lower():
                 print("    [DRIVE] Error de permisos detectado. Verifique que la cuenta de servicio tenga rol de 'Contribuidores' o superior.")
            
            if attempt < max_retries - 1:
                import time
                time.sleep(2) # Esperar un poco antes de reintentar
            else:
                print(f"    [DRIVE] ERROR CRITICO tras {max_retries} intentos.")
                return None

import io
import os
import logging
import functools
import time
import re
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/drive']

def _download_via_api_key(file_id):
    """Fallback de descarga para archivos públicos vía GOOGLE_API_KEY (sin service account).
    Devuelve (bytes, meta) si tiene éxito, o (None, None) si no hay API key o falla."""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None, None
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={api_key}"
    response = requests.get(url)
    if response.status_code == 200:
        # Sin metadata por esta vía; se rellena con valores neutros.
        meta = {"name": "public_file.pdf", "mimeType": "application/pdf"}
        return response.content, meta
    logger.error(f"API Key fallback failed with status {response.status_code}: {response.text}")
    return None, None

def get_drive_service(impersonate_user: str = None):
    possible_paths = [
        "/etc/secrets/service_account",       
        "/etc/secrets/service_account.json",  
        'service_account.json'                
    ]
    
    creds_file = None
    for path in possible_paths:
        if os.path.exists(path):
            creds_file = path
            break
            
    if not creds_file:
        logger.error("No service account file found in any expected location.")
        return None
        
    try:
        creds = service_account.Credentials.from_service_account_file(
            creds_file, scopes=SCOPES)
        logger.info(f"Loaded credentials from {creds_file}")
            
        if impersonate_user:
            try:
                creds = creds.with_subject(impersonate_user)
                logger.info(f"Impersonating user: {impersonate_user}")
            except Exception as e:
                logger.warning(f"Failed to impersonate {impersonate_user}: {str(e)}")
                
        return build('drive', 'v3', credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.error(f"Error building Drive service: {str(e)}")
        return None

def resolve_file_id(file_id_or_path: str) -> str:
    if not file_id_or_path:
        return file_id_or_path
        
    # Pattern for https://drive.google.com/file/d/[ID]/view...
    url_pattern1 = r"/file/d/([a-zA-Z0-9_-]+)"
    # Pattern for https://drive.google.com/open?id=[ID]
    url_pattern2 = r"id=([a-zA-Z0-9_-]+)"
    
    match1 = re.search(url_pattern1, file_id_or_path)
    if match1:
        return match1.group(1)
        
    match2 = re.search(url_pattern2, file_id_or_path)
    if match2:
        return match2.group(1)

    if '/' not in file_id_or_path:
        return file_id_or_path  
    
    filename = file_id_or_path.strip('/').split('/')[-1]
    
    try:
        service = get_drive_service()
        if not service:
            logger.error(f"Unable to resolve File ID for '{file_id_or_path}': Service not available.")
            return file_id_or_path
        
        query = f"name = '{filename}' and trashed = false"
        max_retries = 3
        base_delay = 2
        
        for attempt in range(max_retries):
            logger.info(f"Searching for file with name: '{filename}' (Attempt {attempt+1}/{max_retries})")
            
            results = service.files().list(
                q=query,
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageSize=5
            ).execute()
            
            files = results.get('files', [])
            if files:
                new_id = str(files[0]['id']).strip()
                logger.info(f"Resolved path '{file_id_or_path}' to ID: '{new_id}'")
                return new_id
                
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"No file found in Drive with name '{filename}' yet. Retrying in {delay}s...")
                time.sleep(delay)
                
        logger.warning(f"No file found in Drive with name '{filename}'. Path resolution failed after retries.")
        return file_id_or_path  
    except Exception as e:
        logger.error(f"Excpetion while resolving path '{file_id_or_path}': {str(e)}")
        return file_id_or_path

def download_with_validation(file_id):
    try:
        if not file_id:
            return None, None
            
        # Basic validation
        if '/' in file_id or len(file_id) < 15:
            logger.error(f"Invalid file_id format: '{file_id}'.")
            return None, None
            
        service = get_drive_service()
        if not service:
            # Fallback for public files using API Key if service account is not available
            logger.info(f"Attempting download via API Key fallback for ID: {file_id}")
            return _download_via_api_key(file_id)
        
        logger.info(f"Downloading file with ID: {file_id}")
        meta = service.files().get(
            fileId=file_id, 
            fields="name, mimeType",
            supportsAllDrives=True
        ).execute()
        
        request = service.files().get_media(
            fileId=file_id,
            supportsAllDrives=True
        )
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, request)
        
        done = False
        while done is False:
            _, done = downloader.next_chunk()
            
        file_stream.seek(0)
        return file_stream.read(), meta
    except Exception as e:
        logger.error(f"Error downloading file {file_id}: {str(e)}")
        # If service account failed with 403/404, try API key fallback one last time
        if "403" in str(e) or "404" in str(e):
            logger.info(f"Service account failed. Retrying with API Key for ID: {file_id}")
            content, meta = _download_via_api_key(file_id)
            if content:
                return content, meta
        return None, None

@functools.lru_cache(maxsize=128)
def get_folder_path_from_drive(folder_id: str) -> str:
    try:
        service = get_drive_service()
        if not service:
            return ""
            
        path_parts = []
        current_id = folder_id

        while current_id:
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
                except Exception as e:
                    logger.warning(f"No se pudo obtener el nombre de la unidad compartida '{drive_id}': {e}")
                break
            
            if not parents:
                break
            current_id = parents[0]

        if path_parts:
            return "/".join(path_parts) + "/"
        return ""
    except Exception as e:
        logger.warning(f"No se pudo construir la ruta del folder '{folder_id}': {e}")
        return ""

def upload_image_to_drive(image_bytes, filename, mime_type, folder_id):
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
            
            try:
                service.permissions().create(
                    fileId=file_id,
                    body={'type': 'anyone', 'role': 'reader'},
                    supportsAllDrives=True
                ).execute()
            except Exception as e:
                logger.warning(f"No se pudo asignar permiso público al archivo {file_id}: {e}")

            return file_id

        except Exception as e:
            logger.warning(f"Fallo al subir imagen '{filename}' a Drive (intento {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                return None

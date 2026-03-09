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
        return None
        
    try:
        creds = service_account.Credentials.from_service_account_file(
            creds_file, scopes=SCOPES)
            
        if impersonate_user:
            try:
                creds = creds.with_subject(impersonate_user)
            except:
                pass
                
        return build('drive', 'v3', credentials=creds, cache_discovery=False)
    except:
        return None

def resolve_file_id(file_id_or_path: str) -> str:
    if not file_id_or_path or '/' not in file_id_or_path:
        return file_id_or_path  
    
    filename = file_id_or_path.strip('/').split('/')[-1]
    
    try:
        service = get_drive_service()
        if not service:
            return file_id_or_path
        
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
            return str(files[0]['id']).strip()
        else:
            return file_id_or_path  
    except:
        return file_id_or_path

def download_with_validation(file_id):
    try:
        service = get_drive_service()
        if not service:
            return None, None
        
        meta = service.files().get(fileId=file_id, fields="name, mimeType").execute()
        
        request = service.files().get_media(fileId=file_id)
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, request)
        
        done = False
        while done is False:
            _, done = downloader.next_chunk()
            
        file_stream.seek(0)
        return file_stream.read(), meta
    except:
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
                except:
                    pass
                break
            
            if not parents:
                break
            current_id = parents[0]

        if path_parts:
            return "/".join(path_parts) + "/"
        return ""
    except:
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
            except:
                pass

            return file_id
            
        except:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                return None
